#!/bin/sh
# Back up the gateway's persistent state (#375): ownership registry, prepaid
# bandwidth credit, pool state, allowance and spend counters, and the x402
# audit log. The archives contain bearer credit tokens: treat every copy,
# including the off-host one, as secret.
#
# Install outside the deployed checkout (every deploy re-clones it), e.g.:
#   install -m 0755 scripts/backup_state.sh /usr/local/sbin/swarm_connect_backup
#   cron: 15 3 * * * root BACKUP_REMOTE=... /usr/local/sbin/swarm_connect_backup >> /var/log/swarm_connect_backup.log 2>&1
#
# Environment (all optional):
#   BACKUP_SOURCES    directories to archive (default: /opt/swarm_connect_data /opt/swarm_connect_dev_data)
#   BACKUP_DIR        where archives are written (default: /var/backups/swarm_connect)
#   BACKUP_KEEP_DAYS  local retention in whole days (default: 14)
#   BACKUP_REMOTE     rsync/scp destination for an OFF-HOST copy, e.g. backup@host:/backups/swarm_connect
#                     (root needs an ssh key and a known_hosts entry for it)
#
# Exit status is non-zero if any source failed to archive or copy; the other
# sources are still processed. A successful run touches $BACKUP_DIR/.last_success.
set -u

SOURCES="${BACKUP_SOURCES:-/opt/swarm_connect_data /opt/swarm_connect_dev_data}"
DIR="${BACKUP_DIR:-/var/backups/swarm_connect}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
failed=0

case "$KEEP" in
  ''|*[!0-9]*) echo "BACKUP_KEEP_DAYS must be a whole number of days, got '$KEEP'" >&2; exit 2 ;;
esac

umask 077
mkdir -p "$DIR" || exit 1

for src in $SOURCES; do
  if [ ! -d "$src" ]; then echo "skip: $src does not exist"; continue; fi
  name="$(basename "$src")"
  out="$DIR/${name}-${STAMP}.tar.gz"
  # State files are written atomically (temp file + rename), so a finished file
  # is always consistent; leave the in-flight temp files out. The audit log is
  # append-only, so a copy taken mid-append is at worst missing its last line.
  tar -czf "$out.part" --exclude='.tmp-*' --exclude='*.tmp' -C "$(dirname "$src")" "$name"
  rc=$?
  if [ "$rc" -gt 1 ]; then
    echo "FAILED: archiving $src (tar exit $rc)" >&2
    rm -f "$out.part"; failed=1; continue
  fi
  [ "$rc" -eq 1 ] && echo "warning: a file under $src changed while it was archived (tar exit 1)"
  mv "$out.part" "$out"
  echo "backed up $src -> $out ($(du -h "$out" | cut -f1))"
  if [ -n "${BACKUP_REMOTE:-}" ]; then
    if command -v rsync >/dev/null 2>&1; then rsync -a "$out" "$BACKUP_REMOTE/"; else scp -q "$out" "$BACKUP_REMOTE/"; fi
    if [ $? -ne 0 ]; then echo "FAILED: off-host copy of $out to $BACKUP_REMOTE" >&2; failed=1
    else echo "copied off-host to $BACKUP_REMOTE"; fi
  fi
done

# Only this script's own archives, only in $DIR itself.
find "$DIR" -maxdepth 1 -type f \( -name 'swarm_connect*_data-*.tar.gz' -o -name '*.tar.gz.part' \) \
  -mtime +"$KEEP" -print -delete

if [ "$failed" -eq 0 ]; then
  touch "$DIR/.last_success"
fi
exit "$failed"
