"""Stand-ins for torch, diffusers, transformers and PIL, so render.py runs without them."""
import sys
import types
from contextlib import nullcontext


class OutOfMemoryError(RuntimeError):
    pass


class FakeImage:
    def __init__(self, size=(1024, 1024)):
        self.size = size
        self.saved = []

    def save(self, path, format, quality):
        self.saved.append((str(path), format, quality))
        with open(path, "wb") as f:
            f.write(b"jpg")

    def resize(self, size, resample):
        img = FakeImage(size)
        img.resample = resample
        img.parent = self
        return img


def make_torch(*, cuda=True, free_gib=20.0, nsfw=0.0, peak_gib=10.0, oom_on_to=False):
    cuda_ns = types.SimpleNamespace(
        is_available=lambda: cuda, mem_get_info=lambda: (int(free_gib * 2**30), 2**36),
        reset_peak_memory_stats=lambda: None,
        max_memory_allocated=lambda: int(peak_gib * 2**30), OutOfMemoryError=OutOfMemoryError)

    class Generator:
        def __init__(self, device):
            self.device, self.seed = device, None

        def manual_seed(self, seed):
            self.seed = seed
            return self

    return types.SimpleNamespace(
        __version__="9.9", float16="fp16", cuda=cuda_ns, Generator=Generator,
        no_grad=nullcontext, softmax=lambda logits, dim: {(0, 1): nsfw, (0, 0): 1 - nsfw})


class FakeTokenizer:
    """Counts words; model_max_length 77 like CLIP's."""
    model_max_length = 77

    def __init__(self, per_word=1):
        self.per_word = per_word

    def __call__(self, text, add_special_tokens=False):
        n = len(text.replace(",", " ").split()) * self.per_word
        return types.SimpleNamespace(input_ids=[0] * n)


class FakePipe:
    def __init__(self, torch, image=None, oom=False):
        self.tokenizer = FakeTokenizer(1)
        self.tokenizer_2 = FakeTokenizer(2)
        self.calls, self.image, self.oom = [], image or FakeImage(), oom

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.oom:
            raise OutOfMemoryError("out of memory")
        return types.SimpleNamespace(images=[self.image])


def make_renderer(torch=None, **pipe_kw):
    from leeroy_selfie.render import Renderer
    torch = torch or make_torch()
    processor = lambda images, return_tensors: {}  # noqa: E731
    classifier = lambda **kw: types.SimpleNamespace(logits=None)  # noqa: E731
    return Renderer(FakePipe(torch, **pipe_kw), processor, classifier, 1, torch,
                    {"device": "cuda"})


def stub_pil(monkeypatch):
    image_mod = types.SimpleNamespace(LANCZOS="LANCZOS")
    monkeypatch.setitem(sys.modules, "PIL", types.SimpleNamespace(Image=image_mod))
