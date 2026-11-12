#!/usr/bin/env bash
# Only temporary log fixtures; no installed services or logs.
set -euo pipefail
scripts=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec "${AC_LOG_TEST_PYTHON:-python3}" "$scripts/tests/test_prune_native_logs.py"
