"""Hypothesis profiles for the property tests.

`dev` (the default on a workstation) explores fresh random examples each run with a per-example
deadline; `ci` (chosen when the CI environment variable is set, as on GitHub Actions) is
derandomized, keeps no example database and has no deadline, so a shared runner neither flakes
on timing nor finds a new example in a pull request that did not touch the code; `thorough` is
for a deliberate long run. HYPOTHESIS_PROFILE picks one by name.

    HYPOTHESIS_PROFILE=thorough python -m pytest -q tests/properties

Hypothesis comes with the test extra (pip install -e ".[test]"). A venv installed before it was
listed skips these modules (each one's importorskip) instead of failing the suite's collection;
CI always installs it, so there a missing Hypothesis fails.
"""
import os

try:
    from hypothesis import HealthCheck, settings
except ImportError:
    if os.environ.get("CI"):
        raise
else:
    settings.register_profile("dev", max_examples=100, deadline=1000)
    settings.register_profile("ci", max_examples=100, deadline=None, derandomize=True, database=None,
                              print_blob=True, suppress_health_check=[HealthCheck.too_slow])
    settings.register_profile("thorough", max_examples=2000, deadline=None,
                              suppress_health_check=[HealthCheck.too_slow])
    settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev"))
