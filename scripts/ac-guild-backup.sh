#!/usr/bin/env bash
# Verified off-machine guild backups. Configuration stays in LOCALAPPDATA, outside git.
# Usage: ac-guild-backup.sh [--with-db | --restore-test | --restore-to NEW_DIRECTORY]
set -euo pipefail
scripts=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=${AC_ROOT:-$(cd "$scripts/.." && pwd)}
python=${AC_GUILD_BACKUP_PYTHON:-$root/mind/.venv/Scripts/python.exe}
if [[ ! -x "$python" ]]; then
    printf 'Guild backup needs the mind venv or AC_GUILD_BACKUP_PYTHON.\n' >&2
    exit 1
fi
if command -v cygpath >/dev/null 2>&1; then
    scripts=$(cygpath -w "$scripts")
    root=$(cygpath -w "$root")
fi
exec "$python" "$scripts/guild_backup.py" --root "$root" "$@"
