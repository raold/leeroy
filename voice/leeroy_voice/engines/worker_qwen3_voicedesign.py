"""Qwen3-TTS VoiceDesign as a worker (worker.py): a voice invented from a description, as the
listening kit's design round renders it. It runs in the kit's venv,
private/voice-lab/engines/qwen-tts/.venv, so it imports nothing of leeroy_voice:

    <venv python> -I -X utf8 worker_qwen3_voicedesign.py --model <dir> --device cpu --threads 8

A request's params: `instruct` the description, `gender`, `seed`, `takes`. The kit's mechanical
rule: a voice whose median pitch contradicts the gender is rendered again with the next seed, up
to `takes` times, and the take nearest the range is kept. Nothing is chosen by ear."""
import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
from worker import serve  # noqa: E402

RATE = 24000
PITCH_RATE = 16000
GENDER_F0 = {"male": (0.0, 175.0), "female": (140.0, 1000.0)}  # Hz, median pYIN pitch


def median_pitch(audio, rate: int) -> float | None:
    import librosa
    import numpy as np
    y = librosa.resample(np.asarray(audio, dtype=np.float32).reshape(-1), orig_sr=rate,
                         target_sr=PITCH_RATE)
    f0, voiced, _ = librosa.pyin(y, fmin=60, fmax=400, sr=PITCH_RATE, frame_length=1024)
    v = f0[voiced & ~np.isnan(f0)]
    return float(np.median(v)) if v.size else None


def miss(f0: float | None, gender: str) -> float:
    """How far the pitch is outside the gender's range; 0 inside it."""
    lo, hi = GENDER_F0[gender]
    if f0 is None:
        return float("inf")
    return max(lo - f0, f0 - hi, 0.0)


def load(model: Path, device: str, threads: int):
    import torch
    from qwen_tts import Qwen3TTSModel

    torch.set_num_threads(threads)
    tts = Qwen3TTSModel.from_pretrained(str(model), device_map=device,
                                        dtype=torch.bfloat16 if device == "cuda" else torch.float32)

    def speak(text: str, ref: str, params: dict):
        best = None
        for take in range(max(1, params["takes"])):
            torch.manual_seed(params["seed"] + take)
            wavs, rate = tts.generate_voice_design(text=text, language="English",
                                                   instruct=params["instruct"])
            off = miss(median_pitch(wavs[0], rate), params["gender"])
            if best is None or off < best[0]:
                best = (off, wavs[0])
            if off == 0.0:
                break
        return best[1]
    return speak, RATE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--device", choices=("cuda", "cpu"), required=True)
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()
    return serve(lambda: load(args.model, args.device, args.threads))


if __name__ == "__main__":
    sys.exit(main())
