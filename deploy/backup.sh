#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${AGE_RECIPIENT:?Missing AGE_RECIPIENT}"
temporary=$(mktemp -d "${SONGFINDER_DATA:-/var/lib/songfinder}/backup.XXXXXX")
trap 'rm -rf -- "$temporary"' EXIT
mkdir -m 700 "$temporary/db"
"/opt/songfinder/.venv/bin/python" -m songfinder.maintenance "$temporary/db/jobs.sqlite3"
tar -C "$temporary/db" -cf "$temporary/database.tar" .
age -r "$AGE_RECIPIENT" -o "$temporary/database.tar.age" "$temporary/database.tar"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
rclone --config "/etc/songfinder-backup.conf" copyto "$temporary/database.tar.age" "backups:site-backups/songfinder/$stamp.tar.age"
rm -rf -- "$temporary"
trap - EXIT
if [[ -n "${HC_BACKUP_URL:-}" ]]; then curl --max-time 20 -fsS "$HC_BACKUP_URL" >/dev/null; fi
