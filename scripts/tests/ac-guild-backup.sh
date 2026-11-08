#!/usr/bin/env bash
# Standard-library tests use only generated files beneath TemporaryDirectory.
set -euo pipefail
scripts=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python=${AC_GUILD_BACKUP_PYTHON:-python3}
exec "$python" "$scripts/tests/test_guild_backup.py"
