#!/usr/bin/env bash
# ac-soap.py against a fake SOAP server on loopback; no worldserver, no real credentials.
set -euo pipefail
scripts=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec "${AC_SOAP_TEST_PYTHON:-python3}" "$scripts/tests/test_ac_soap.py"
