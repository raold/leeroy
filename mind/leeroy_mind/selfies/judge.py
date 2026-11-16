"""The photo critic's vision judge: one vision model on Ollama ([selfies] critic_model, pulled by
the operator, never by the mind) answers a fixed JSON schema about one rendered photo and, when
there is one, her reference photo: whether her limbs, hands, fingers and eyes are whole, whether
anyone else is in it, whether it is the same woman, and an overall grade 0-3.

It runs only where the GPU is already the night's, the booth's or a photo request's, after the
selfie worker's process tree has exited (its VRAM is free). A model Ollama does not list is
skipped with one log line (critic_judge_absent); a GPU pause, a failed or malformed answer
skips that photo (critic_judge_skipped code=), never retried in a loop. The photos go to the
model as the message's images; the answer is parsed into codes and booleans and nothing else is
kept: no text the model writes is logged or stored, nor a path, nor an image byte."""
import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.llm.client import GpuPaused, LLMError
from leeroy_mind.logs import log

MODEL = "qwen2.5vl:7b"
TIMEOUT_S = 120.0
KEEP_ALIVE = "60s"  # long enough for the pass, short enough to leave the GPU soon after
MAX_IMAGE_BYTES = 8 * 1024 * 1024
LIMBS = ("ok", "defect")
HANDS = ("ok", "defect", "not_visible")
SCHEMA = {
    "type": "object",
    "properties": {
        "limbs": {"type": "string", "enum": list(LIMBS)},
        "hands": {"type": "string", "enum": list(HANDS)},
        "fingers_ok": {"type": "boolean"},
        "eyes_ok": {"type": "boolean"},
        "extra_people": {"type": "boolean"},
        "same_person": {"type": "boolean"},
        "overall": {"type": "integer", "enum": [0, 1, 2, 3]},
    },
    "required": ["limbs", "hands", "fingers_ok", "eyes_ok", "extra_people", "same_person",
                 "overall"],
}
PROMPT = (
    "The first image is an AI-generated photo of one adult woman. Check it for rendering "
    "defects. Look closely at her arms, legs, hands, fingers, feet and eyes.\n"
    'limbs: "defect" if an arm or leg is missing, merged with another, extra, cut off where it '
    'should continue, or a stray limb appears; else "ok".\n'
    'hands: "defect" if a hand is fused, melted, twisted, has extra or missing parts, or merges '
    'into an object; "not_visible" if no hand shows; else "ok".\n'
    "fingers_ok: false if any visible hand has more or fewer than five fingers or fingers fused "
    "together.\n"
    "eyes_ok: false if one eye looks a different way from the other, the eyes are uneven, or an "
    "eye is malformed.\n"
    "extra_people: true if any other person, face or body part of someone else appears (in a "
    "mirror, screen or picture too).\n"
    "same_person: {same}\n"
    "overall: 0 badly broken, 1 a clear defect, 2 a small flaw, 3 clean and natural.\n"
    "Answer with the JSON object only.")
SAME_WITH_REF = "true if the second image shows the same woman as the first."
SAME_WITHOUT_REF = "true (there is no second image)."
OPTIONS = {"temperature": 0, "num_predict": 160, "num_ctx": 6144}


@dataclass(frozen=True)
class Judged:
    limbs: str
    hands: str
    fingers_ok: bool
    eyes_ok: bool
    extra_people: bool
    same_person: bool | None  # None: judged without a reference
    overall: int

    @property
    def codes(self) -> tuple[str, ...]:
        """The defects it names, in a fixed order."""
        out = []
        if self.limbs == "defect":
            out.append("limbs")
        if self.hands == "defect":
            out.append("hands")
        if not self.fingers_ok:
            out.append("fingers")
        if not self.eyes_ok:
            out.append("eyes")
        if self.extra_people:
            out.append("extra_people")
        return tuple(out)


def parse(text: str, *, with_ref: bool) -> Judged:
    """The model's answer as Judged; ValueError for anything off the schema."""
    try:
        d = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise ValueError("judge_not_json") from e
    if type(d) is not dict or not set(SCHEMA["required"]) <= set(d):
        raise ValueError("judge_fields")
    if d["limbs"] not in LIMBS or d["hands"] not in HANDS:
        raise ValueError("judge_enum")
    if any(type(d[k]) is not bool for k in ("fingers_ok", "eyes_ok", "extra_people",
                                           "same_person")):
        raise ValueError("judge_bool")
    if type(d["overall"]) is not int or not 0 <= d["overall"] <= 3:
        raise ValueError("judge_overall")
    return Judged(d["limbs"], d["hands"], d["fingers_ok"], d["eyes_ok"], d["extra_people"],
                  d["same_person"] if with_ref else None, d["overall"])


def _has(names, model: str) -> bool:
    return model in names or (":" not in model and f"{model}:latest" in names)


def _image(path: Path) -> str:
    data = Path(path).read_bytes()
    if not data.startswith(b"\xff\xd8\xff") or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("judge_image")
    return base64.b64encode(data).decode("ascii")


class VisionJudge:
    """Ask `llm` (an LLMClient whose chat passes a message's images through) about photos.
    `options` adds to OPTIONS (the validation run on the CPU passes num_gpu 0). `paused` turns
    true at the first GpuPaused: the rest of the pass is skipped."""

    def __init__(self, llm, model: str = MODEL, *, options: dict | None = None,
                 timeout: float = TIMEOUT_S, keep_alive=KEEP_ALIVE):
        self.llm = llm
        self.model = model
        self.options = {**OPTIONS, **(options or {})}
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.paused = False
        self.used = False  # a request was sent: its model may be loaded
        self._present: bool | None = None

    async def available(self) -> bool:
        """Whether Ollama lists the model; asked once per judge, logged once when absent."""
        if self._present is None:
            try:
                self._present = _has(await self.llm.list_models(), self.model)
            except GpuPaused:
                self.paused = True
                return False
            except (LLMError, OSError) as e:
                log("critic_judge_unavailable", level=logging.WARNING, error=type(e).__name__)
                return False
            if not self._present:
                log("critic_judge_absent", level=logging.WARNING)
        return bool(self._present) and not self.paused

    async def judge(self, image: Path, ref: Path | None = None) -> Judged | None:
        """The judge's answer about `image` (with `ref` as the second image), or None: the
        model absent, the GPU paused, or an answer that failed or broke the schema."""
        if not await self.available():
            return None
        try:
            images = [_image(image)] + ([_image(ref)] if ref is not None else [])
        except (OSError, ValueError):
            log("critic_judge_skipped", code="image")
            return None
        prompt = PROMPT.replace("{same}", SAME_WITH_REF if ref is not None else SAME_WITHOUT_REF)
        self.used = True
        try:
            result = await self.llm.chat(
                self.model, [{"role": "user", "content": prompt, "images": images}],
                options=dict(self.options), think=False, format=SCHEMA,
                keep_alive=self.keep_alive, timeout=self.timeout)
        except GpuPaused:
            self.paused = True
            log("critic_judge_skipped", code="gpu_paused")
            return None
        except (LLMError, OSError, TimeoutError) as e:
            log("critic_judge_skipped", code=type(e).__name__)
            return None
        try:
            return parse(result.text, with_ref=ref is not None)
        except ValueError as e:
            log("critic_judge_skipped", code=str(e))
            return None
