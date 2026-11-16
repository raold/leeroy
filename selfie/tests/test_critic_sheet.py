"""The critic's detector calls against the labelled snap-anatomy sheet (the controller's GPU A/B
of 2026-11-16, D19): LEEROY_CRITIC_SHEET names the folder with sheet.md and its images. Skipped
without the sheet or the fetched detectors. It prints precision and recall of the detectors'
defect call (the mind's selfies/critic.py detector_codes: a second face, no face, more than two
hands, eyes off level) and of the softer hand signal (a hand with fewer than four distinct
fingers or an implausible shape), against the sheet's anatomy labels (H hands, F feet, L limbs,
E eyes; P props are not anatomy). LEEROY_CRITIC_REPORT, when set, receives the per-image JSON."""
import json
import os
import re
from pathlib import Path

import pytest

from leeroy_selfie import critic, models

pytestmark = pytest.mark.engines

EYE_TILT_MAX, EYE_LEVEL_MAX = 8.0, 0.12  # the mind's critic.EYE_TILT_MAX / EYE_LEVEL_MAX
ANATOMY = {"H", "F", "L", "E"}


def labels(sheet: Path) -> dict[str, set[str]]:
    """image name -> its defect codes, for every labelled image whose file is there."""
    out = {}
    for line in (sheet / "sheet.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| (\d+_[A-G]_\d+\.jpg) \|", line)
        if not m or not (sheet / m.group(1)).is_file():
            continue
        verdict = [c.strip() for c in line.strip().strip("|").split("|")][-1]
        out[m.group(1)] = set(re.findall(r"\b([HFLEP])1\??", verdict.split("(")[0]))
    return out


def detector_call(m: dict) -> bool:
    eyes = m.get("eyes") or {}
    return bool((m["faces"] is not None and m["faces"] != 1)
                or (m["hands"] is not None and m["hands"] > 2)
                or (eyes.get("tilt") or 0) > EYE_TILT_MAX or (eyes.get("level") or 0) > EYE_LEVEL_MAX)


def hand_call(m: dict) -> bool:
    return any(h["fingers"] < 4 or not h["plausible"] for h in m["hand"])


def rates(calls: dict[str, bool], truth: dict[str, bool]) -> dict:
    tp = sum(calls[k] and truth[k] for k in truth)
    fp = sum(calls[k] and not truth[k] for k in truth)
    fn = sum(truth[k] and not calls[k] for k in truth)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": len(truth) - tp - fp - fn,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None}


def test_the_detectors_against_the_labelled_sheet(fetched_data_dir):
    sheet = Path(os.environ.get("LEEROY_CRITIC_SHEET", ""))
    if not os.environ.get("LEEROY_CRITIC_SHEET") or not (sheet / "sheet.md").is_file():
        pytest.skip("LEEROY_CRITIC_SHEET does not name the labelled sheet")
    if not all(ok is True for name in ("yunet",) + models.CRITIC_PINS
               for ok, _ in models.check(fetched_data_dir, name, quick=True)):
        pytest.skip("the critic's detectors are not fetched")
    det = critic.load_detectors(fetched_data_dir)
    truth_codes = labels(sheet)
    assert len(truth_codes) >= 70
    measured = {name: critic.measure(det, str(sheet / name)) for name in truth_codes}
    assert all(m["detectors"] == {"faces": "ok", "hands": "ok"} for m in measured.values())
    truth = {k: bool(v & ANATOMY) for k, v in truth_codes.items()}
    hands_truth = {k: "H" in v for k, v in truth_codes.items()}
    eyes_truth = {k: "E" in v for k, v in truth_codes.items()}
    report = {
        "images": len(truth), "defective": sum(truth.values()),
        "detector_call": rates({k: detector_call(m) for k, m in measured.items()}, truth),
        "hand_signal_vs_any_defect": rates({k: hand_call(m) for k, m in measured.items()}, truth),
        "hand_signal_vs_hand_defect": rates({k: hand_call(m) for k, m in measured.items()},
                                            hands_truth),
        "eyes_tilt_vs_eye_defect": rates(
            {k: (m["eyes"] or {}).get("tilt", 0) > EYE_TILT_MAX for k, m in measured.items()},
            eyes_truth)}
    print(json.dumps(report, indent=1))
    if os.environ.get("LEEROY_CRITIC_REPORT"):
        Path(os.environ["LEEROY_CRITIC_REPORT"]).write_text(json.dumps(
            {"summary": report, "images": {k: {"labels": sorted(truth_codes[k]), **m}
                                           for k, m in measured.items()}}, indent=1))
