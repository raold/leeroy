#!/usr/bin/env bash
# Dump all four AzerothCore databases.
#
# Usage: ac-backup.sh [label]      e.g. ac-backup.sh pre_core_update
#   AC_KEEP=N   how many backup sets to retain (default 5)

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

LABEL="${1:-manual}"
KEEP="${AC_KEEP:-5}"
DUMP="$MYSQL_DIR/mysqldump.exe"
[ -x "$DUMP" ] || die "mysqldump not found at $DUMP (set MYSQL_DIR)"

ac_db_creds WorldDatabaseInfo

OUT="$AC_BACKUP/${LABEL}_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"
log "backing up to $OUT"

for db in acore_auth acore_characters acore_world acore_playerbots; do
    if ! "$DUMP" -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" -p"$DB_PASS" \
            --single-transaction --routines --triggers --events \
            --default-character-set=utf8mb4 "$db" > "$OUT/$db.sql" 2>"$OUT/$db.err"; then
        warn "$db failed: $(tail -1 "$OUT/$db.err")"
        continue
    fi
    rm -f "$OUT/$db.err"
    printf '    %-20s %s MB\n' "$db" "$(du -m "$OUT/$db.sql" | cut -f1)"
done

# Prune ONLY sets this script created. db_backup/ also holds hand-made backups
# and a raw datadir copy; those are not ours to delete. Our sets are always
# "<label>_<YYYYmmdd>_<HHMMSS>" -- anything else is left alone.
log "pruning to the newest $KEEP sets created by this script"
ls -1dt "$AC_BACKUP"/*/ 2>/dev/null \
  | grep -E '_[0-9]{8}_[0-9]{6}/$' \
  | tail -n +$((KEEP + 1)) \
  | while read -r old; do
        log "  removing $(basename "$old")"
        rm -rf "$old"
    done

log "done: $OUT"
