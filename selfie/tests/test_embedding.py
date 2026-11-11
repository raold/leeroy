"""Pinned encoder protocol and CPU fallback with fake torch/transformers only."""
from contextlib import contextmanager
import json
import math
import sys
from types import SimpleNamespace

import pytest

from fakes import FakeImage, OutOfMemoryError
from leeroy_selfie import embedding, protocol
from leeroy_selfie.render import ReadyError
from test_worker import run


def engine(monkeypatch, *, free=0, cuda=True, placement_oom=False, encode_oom=False):
    calls, placements, grad = [],[],[False]
    @contextmanager
    def no_grad():
        grad[0] = True
        try:
            yield
        finally:
            grad[0] = False
    class Tensor:
        def to(self,**kw):
            calls.append(("pixels",kw))
            return self
        def float(self): return self
        def cpu(self): return self
        def tolist(self): return [3.0,4.0]+[0.0]*1022
    class Encoder:
        device,dtype = "cpu","fp32"
        failed = False
        def eval(self): return self
        def to(self,**kw):
            placements.append(kw)
            if placement_oom and kw["device"] == "cuda":
                raise OutOfMemoryError("private")
            self.device,self.dtype = kw["device"],kw["dtype"]
            return self
        def __call__(self,pixels):
            assert grad[0]
            if encode_oom and not self.failed:
                self.failed = True
                raise OutOfMemoryError("private")
            return SimpleNamespace(image_embeds=[Tensor()])
    encoder = Encoder()
    def pretrained(path,**kw):
        calls.append(("load",str(path),kw))
        return encoder
    def processor(**kwargs):
        assert kwargs == {"size":224,"crop_size":224}
        def call(*,images,return_tensors):
            assert grad[0] and return_tensors == "pt"
            calls.append(("crop",images.box))
            return SimpleNamespace(pixel_values=Tensor())
        return call
    torch = SimpleNamespace(float16="fp16",float32="fp32",__version__="fake",no_grad=no_grad,
        cuda=SimpleNamespace(is_available=lambda:cuda,mem_get_info=lambda:(free*2**30,24*2**30),
            empty_cache=lambda:calls.append(("empty_cache",)),OutOfMemoryError=OutOfMemoryError))
    monkeypatch.setitem(sys.modules,"torch",torch)
    monkeypatch.setitem(sys.modules,"transformers",SimpleNamespace(CLIPImageProcessor=processor,
        CLIPVisionModelWithProjection=SimpleNamespace(from_pretrained=pretrained)))
    monkeypatch.setattr(embedding.models,"check",lambda *a,**kw:[(True,"present")])
    monkeypatch.setattr(embedding.jpeg,"open_rgb",lambda path:FakeImage())
    return encoder,calls,placements


@pytest.mark.parametrize("free,cuda,oom,device", [(1,True,False,"cpu"),(20,True,False,"cuda"),
    (20,False,False,"cpu"),(20,True,True,"cpu")])
def test_only_cached_encoder_loads_with_cpu_fallback(monkeypatch,tmp_path,free,cuda,oom,device):
    encoder,calls,placements = engine(monkeypatch,free=free,cuda=cuda,placement_oom=oom)
    loaded = embedding.load(tmp_path,min_free_gib=11.5)
    assert loaded.info["device"] == device and encoder.device == device
    loads = [c for c in calls if c[0] == "load"]
    assert len(loads) == 1 and loads[0][2]["local_files_only"] is True
    assert loads[0][1].endswith("image_encoder")
    assert loaded.embedding_only and not loaded.identity
    answer = loaded.embed(protocol.Embed("e1",str(tmp_path/"a.jpg")))
    assert len(answer["vector"]) == 1024 and answer["vector"][:2] == [.6,.8]
    assert answer["model"] == embedding.MODEL and answer["device"] == device
    assert ("crop",(128,51,896,819)) in calls


def test_oom_while_embedding_retries_on_cpu(monkeypatch,tmp_path):
    encoder,calls,placements = engine(monkeypatch,free=20,encode_oom=True)
    loaded = embedding.load(tmp_path,min_free_gib=11.5)
    result = loaded.embed(protocol.Embed("e1",str(tmp_path/"a.jpg")))
    assert result["device"] == "cpu"
    assert placements[-1] == {"device":"cpu","dtype":"fp32"}


def test_missing_pin_does_not_load_or_download_anything(monkeypatch,tmp_path):
    monkeypatch.setattr(embedding.models,"check",lambda *a,**kw:[(None,"missing")])
    monkeypatch.setitem(sys.modules,"torch",None)
    with pytest.raises(ReadyError,match="encoder_unavailable"):
        embedding.load(tmp_path,min_free_gib=11.5)


def test_embed_protocol_is_strict_and_worker_does_not_print_vectors_to_logs(monkeypatch,tmp_path,capsys):
    engine(monkeypatch)
    renderer = embedding.load(tmp_path,min_free_gib=11.5)
    req = {"id":"e1","op":"embed","ref":str(tmp_path/"a.jpg")}
    assert isinstance(protocol.parse(json.dumps(req).encode()),protocol.Embed)
    code,lines = run(lambda:renderer,json.dumps(req).encode()+b"\n")
    assert code == 0 and lines[1]["ok"] and len(lines[1]["vector"]) == 1024
    assert "vector" not in capsys.readouterr().err
    for bad in (dict(req,extra=1),dict(req,ref="relative.jpg"),dict(req,ref=str(tmp_path/"a.png"))):
        with pytest.raises(ValueError):
            protocol.parse(json.dumps(bad).encode())


def test_embedding_failure_returns_only_fixed_code(tmp_path,capsys):
    def fail(req):
        raise ValueError("private image and persona content")
    renderer = SimpleNamespace(identity=False,info={},embed=fail)
    code,lines = run(lambda:renderer,json.dumps({"id":"e","op":"embed","ref":str(tmp_path/"a.jpg")}).encode()+b"\n")
    assert lines[1] == {"id":"e","ok":False,"error":"encoder_failed"}
    assert "private" not in capsys.readouterr().err
