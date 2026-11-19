"""Opt-in FLUX.1-dev renderer. Pinned local weights, NF4 transformer/T5, CPU offload.

Uses the shared scoring, JPEG writes, face crop/cache, captions, grids and detail pass.
The SDXL adapter is never used here. Model imports stay inside load().
"""
from pathlib import Path
import time

from . import models
from .render import RETRY_WAIT_S, ReadyError, Renderer, _face_ref, _free_gib, _ms

DISTILLED_GUIDANCE = 3.5


class FluxRenderer(Renderer):
    def _encode_reference(self, crop, cfg: bool):
        # FLUX keeps positive/negative embeds separately; no SDXL CFG concatenation.
        return self.pipe.prepare_ip_adapter_image_embeds(
            ip_adapter_image=crop, ip_adapter_image_embeds=None,
            device="cuda", num_images_per_prompt=1)

    def _draw(self, pipe, **kwargs):
        cfg = kwargs.pop("guidance_scale")
        # `guidance_scale` alone does NOT apply the negative prompt in FLUX. Keep the
        # protocol's CFG as true CFG, including during img2img, and its trained guidance
        # at the model's standard value. All prompts still fit the shared CLIP window.
        if self.identity and "ip_adapter_image_embeds" in kwargs and cfg > 1:
            kwargs["negative_ip_adapter_image_embeds"] = self._blank(True)
        return pipe(**kwargs, true_cfg_scale=cfg, guidance_scale=DISTILLED_GUIDANCE,
                    max_sequence_length=512)

    def _img2img(self):
        if self._i2i is None:
            from diffusers import FluxImg2ImgPipeline
            self._i2i = FluxImg2ImgPipeline(**self.pipe.components)
            self._i2i.enable_model_cpu_offload()
            self._i2i.set_progress_bar_config(disable=True)
        return self._i2i


def load(data: Path, *, min_free_gib: float, device: str = "cuda",
         identity: bool = False, _wait_s: float = 0.0) -> Renderer:
    try:
        import torch
    except ImportError as exc:
        raise ReadyError("cuda") from exc
    if device != "cuda" or not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise ReadyError("cuda")
    start = time.perf_counter()
    pins = models.FLUX_BASE_PINS + (models.FLUX_IDENTITY_PINS if identity else ())
    for name in pins:
        if any(ok is not True for ok, _ in models.check(data, name, quick=True)):
            raise ReadyError("model", fields={"check_ms": _ms(time.perf_counter() - start),
                                              "load_ms": 0})
    check_ms = _ms(time.perf_counter() - start)
    # Quantization uses the GPU while loading. Admit it before that allocation; a VRAM
    # refusal hasn't loaded weights and retries only after the mind has made room.
    free = _free_gib(torch, min_free_gib, _wait_s)
    fields = {"check_ms": check_ms, "load_ms": 0, "free_gib": round(free, 1)}
    if free < min_free_gib:
        raise ReadyError("vram", fields=fields, retry=lambda: load(
            data, min_free_gib=min_free_gib, device=device, identity=identity,
            _wait_s=RETRY_WAIT_S))
    started_load = time.perf_counter()
    try:
        import diffusers
        from diffusers import BitsAndBytesConfig, FluxPipeline, FluxTransformer2DModel
        from transformers import (AutoImageProcessor, AutoModelForImageClassification,
                                  BitsAndBytesConfig as TextQuant, CLIPImageProcessor,
                                  CLIPVisionModelWithProjection, T5EncoderModel)
        folder = models.model_dir(data, "flux1-dev")
        quant = dict(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                     bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
        transformer = FluxTransformer2DModel.from_pretrained(
            folder / "transformer", quantization_config=BitsAndBytesConfig(**quant),
            torch_dtype=torch.bfloat16, local_files_only=True)
        text_encoder = T5EncoderModel.from_pretrained(
            folder / "text_encoder_2", quantization_config=TextQuant(**quant),
            torch_dtype=torch.bfloat16, local_files_only=True)
        adapter = {}
        if identity:
            encoder_dir = models.model_dir(data, "flux-clip")
            adapter = {
                "image_encoder": CLIPVisionModelWithProjection.from_pretrained(
                    encoder_dir, torch_dtype=torch.bfloat16, local_files_only=True),
                "feature_extractor": CLIPImageProcessor.from_pretrained(
                    encoder_dir, local_files_only=True)}
        pipe = FluxPipeline.from_pretrained(
            folder, transformer=transformer, text_encoder_2=text_encoder,
            torch_dtype=torch.bfloat16, local_files_only=True, **adapter)
        if identity:
            pipe.load_ip_adapter(str(models.model_dir(data, "flux-ipadapter")),
                                 weight_name="ip_adapter.safetensors",
                                 image_encoder_pretrained_model_name_or_path=None,
                                 local_files_only=True)
        pipe.enable_model_cpu_offload()
        pipe.vae.enable_slicing()
        pipe.vae.enable_tiling()
        pipe.set_progress_bar_config(disable=True)
        nsfw_dir = models.model_dir(data, "nsfw")
        processor = AutoImageProcessor.from_pretrained(nsfw_dir, local_files_only=True)
        classifier = AutoModelForImageClassification.from_pretrained(
            nsfw_dir, local_files_only=True).eval()
        labels = {str(v).lower(): int(k) for k, v in classifier.config.id2label.items()}
        if "nsfw" not in labels:
            raise ValueError("classifier_label")
    except torch.cuda.OutOfMemoryError as exc:
        raise ReadyError("vram", fields=fields) from exc
    except Exception as exc:
        raise ReadyError("model", fields=fields) from exc
    info = {**fields, "load_ms": _ms(time.perf_counter() - started_load), "device": "cuda",
            "versions": {"torch": torch.__version__, "diffusers": diffusers.__version__,
                         "backend": "flux1-dev", "quantization": "nf4"}}
    return FluxRenderer(pipe, processor, classifier, labels["nsfw"], torch, info, identity,
                        _face_ref(data) if identity else None, data)
