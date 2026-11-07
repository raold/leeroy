"""Stand-ins for torch, diffusers, transformers and PIL, so render.py runs without them."""
import sys
import types
from contextlib import contextmanager, nullcontext

JPEG = bytes.fromhex(  # an 8 x 8 JPEG (P2's, as the mind's fake worker writes it)
    "ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09"
    "090a140e0f0c1017141818171416161a1d251f1a1b231c1616202c20232627292a29191f2d302d2830252829"
    "28ffdb0043010707070a080a130a0a13281a161a28282828282828282828282828282828282828282828282828"
    "282828282828282828282828282828282828282828ffc00011080008000803012200021101031101ffc4001f00"
    "00010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504"
    "040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a"
    "161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a7374"
    "75767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4"
    "c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101"
    "010101010101010000000000000102030405060708090a0bffc400b511000201020404030407050404000102"
    "77000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f1"
    "1718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a7374757677"
    "78797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6"
    "c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f"
    "00dda28a2bcc3d93ffd9")
OPENED_SIZE = (1024, 1024)  # what the fake PIL says every JPEG it opens measures


class OutOfMemoryError(RuntimeError):
    pass


class FakeImage:
    format = "JPEG"

    def __init__(self, size=(1024, 1024), mode="RGB"):
        self.size, self.mode = size, mode
        self.saved, self.pasted = [], []

    def save(self, path, format, quality):
        self.saved.append((str(path), format, quality))
        with open(path, "wb") as f:
            f.write(b"jpg")

    def resize(self, size, resample):
        img = FakeImage(size)
        img.resample = resample
        img.parent = self
        return img

    def convert(self, mode):
        img = FakeImage(self.size, mode)
        img.parent = self
        return img

    def crop(self, box):
        img = FakeImage((box[2] - box[0], box[3] - box[1]))
        img.box, img.parent = box, self
        return img

    def paste(self, img, box):
        self.pasted.append((img, box))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_torch(*, cuda=True, free_gib=20.0, nsfw=0.0, peak_gib=10.0, oom_on_to=False):
    cuda_ns = types.SimpleNamespace(
        is_available=lambda: cuda, mem_get_info=lambda: (int(free_gib * 2**30), 2**36),
        reset_peak_memory_stats=lambda: None,
        max_memory_allocated=lambda: int(peak_gib * 2**30), OutOfMemoryError=OutOfMemoryError)
    grad_off = [0]

    @contextmanager
    def no_grad():
        grad_off[0] += 1
        try:
            yield
        finally:
            grad_off[0] -= 1

    class Generator:
        def __init__(self, device):
            self.device, self.seed = device, None

        def manual_seed(self, seed):
            self.seed = seed
            return self

    return types.SimpleNamespace(
        __version__="9.9", float16="fp16", cuda=cuda_ns, Generator=Generator,
        no_grad=no_grad, grad_is_off=lambda: grad_off[0] > 0,
        softmax=lambda logits, dim: {(0, 1): nsfw, (0, 0): 1 - nsfw})


class FakeTokenizer:
    """Counts words; model_max_length 77 like CLIP's."""
    model_max_length = 77

    def __init__(self, per_word=1):
        self.per_word = per_word

    def __call__(self, text, add_special_tokens=False):
        n = len(text.replace(",", " ").split()) * self.per_word
        return types.SimpleNamespace(input_ids=[0] * n)


class FakePipe:
    """Records each call's keywords (width, height, ip_adapter_image_embeds...), each embeds
    computation (its keywords, and whether gradients were off) and each adapter scale."""

    def __init__(self, torch, image=None, oom=False):
        self.torch = torch
        self.tokenizer = FakeTokenizer(1)
        self.tokenizer_2 = FakeTokenizer(2)
        self.calls, self.image, self.oom = [], image or FakeImage(), oom
        self.embed_calls, self.embedded, self.scales = 0, [], []

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.oom:
            raise OutOfMemoryError("out of memory")
        return types.SimpleNamespace(images=[self.image])

    def prepare_ip_adapter_image_embeds(self, **kw):
        self.embed_calls += 1
        grad_is_off = getattr(self.torch, "grad_is_off", lambda: None)
        self.embedded.append({**kw, "grad_off": grad_is_off()})
        return [f"embeds-{self.embed_calls}"]

    def set_ip_adapter_scale(self, scale):
        self.scales.append(scale)


def make_renderer(torch=None, *, identity=False, **pipe_kw):
    from leeroy_selfie.render import Renderer
    torch = torch or make_torch()
    processor = lambda images, return_tensors: {}  # noqa: E731
    classifier = lambda **kw: types.SimpleNamespace(logits=None)  # noqa: E731
    return Renderer(FakePipe(torch, **pipe_kw), processor, classifier, 1, torch,
                    {"device": "cuda"}, identity=identity)


def fake_open(path):
    """A FakeImage of OPENED_SIZE for a file that starts like a JPEG; OSError as PIL's for
    anything else (or no file)."""
    with open(path, "rb") as f:
        head = f.read(3)
    if head != JPEG[:3]:
        raise OSError(f"cannot identify image file {str(path)!r}")
    img = FakeImage(OPENED_SIZE)
    img.path = str(path)
    return img


def stub_pil(monkeypatch):
    """A PIL whose Image opens files with fake_open and records each canvas it makes in
    `made`; returns that Image namespace."""
    made = []

    def new(mode, size, color):
        img = FakeImage(size, mode)
        img.color = color
        made.append(img)
        return img

    image_mod = types.SimpleNamespace(LANCZOS="LANCZOS", open=fake_open, new=new, made=made)
    monkeypatch.setitem(sys.modules, "PIL", types.SimpleNamespace(Image=image_mod))
    return image_mod
