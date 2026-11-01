"""Chatterbox Turbo as a worker (worker.py), as the listening kit renders it. It runs under its own
venv, voice-data/engines/chatterbox-turbo/.venv, never the service's, so it imports nothing of
leeroy_voice:

    <venv python> -I -X utf8 worker_chatterbox_turbo.py --model <dir> --device cuda --threads 6

A voice is the member's reference clip, read once and kept for the worker's life. A tag the
tokenizer does not list is dropped, as the kit does, since Turbo would read it out."""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
from worker import serve  # noqa: E402

SAMPLING = ("temperature", "top_p", "top_k", "repetition_penalty")  # all Turbo's generate() uses
TAG = re.compile(r"\[([a-z ]+)\]")


def known_tags(text: str, known: set[str]) -> str:
    for tag in set(TAG.findall(text)) - known:
        text = text.replace(f"[{tag}]", "")
    return " ".join(text.split())


def load(model: Path, device: str, threads: int):
    import torch
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    torch.set_num_threads(threads)
    added = json.loads((model / "added_tokens.json").read_text(encoding="utf-8"))
    known = {tag.strip("[]") for tag in added}
    tts = ChatterboxTurboTTS.from_local(model, device)
    voices = {}

    def speak(text: str, ref: str, params: dict):
        key = (ref, os.stat(ref).st_mtime_ns)
        with torch.inference_mode():
            if key not in voices:
                tts.prepare_conditionals(ref)
                voices[key] = tts.conds
            tts.conds = voices[key]
            wav = tts.generate(known_tags(text, known),
                               **{k: params[k] for k in SAMPLING if k in params})
        return wav.squeeze(0).cpu().numpy()
    return speak, tts.sr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--device", choices=("cuda", "cpu"), required=True)
    ap.add_argument("--threads", type=int, default=6)
    args = ap.parse_args()
    return serve(lambda: load(args.model, args.device, args.threads))


if __name__ == "__main__":
    sys.exit(main())
