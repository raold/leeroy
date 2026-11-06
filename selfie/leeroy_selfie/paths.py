"""Where the worker keeps its models and caches."""
import os
from pathlib import Path


def data_dir() -> Path:
    """LEEROY_SELFIE_DATA, else selfie-data in the checkout (never committed)."""
    env = os.environ.get("LEEROY_SELFIE_DATA")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "selfie-data"
