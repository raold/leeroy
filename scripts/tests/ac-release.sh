#!/usr/bin/env bash
# Git repositories, databases, executables and services below are all synthetic.
set -euo pipefail
scripts=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
AC_RELEASE_TEST_BASH="${BASH:-bash}"
if command -v cygpath >/dev/null 2>&1; then AC_RELEASE_TEST_BASH=$(cygpath -w "$AC_RELEASE_TEST_BASH"); fi
export AC_RELEASE_TEST_BASH
exec "${AC_RELEASE_TEST_PYTHON:-python3}" "$scripts/tests/test_ac_release.py"
