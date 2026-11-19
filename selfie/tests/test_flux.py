"""FLUX plumbing without model files, network or GPU: guidance, identity and load admission."""
import os
import sys
import types

import pytest

from fakes import JPEG, FakePipe, make_renderer, make_torch, stub_pil
from leeroy_selfie import flux, models, render
from leeroy_selfie.__main__ import main
from leeroy_selfie.protocol import Identity
from leeroy_selfie.fit import PromptTooLong
from test_models import put
from test_render import request


def renderer(*, identity=False, score=0.05):
    old = make_renderer(make_torch(nsfw=score), identity=identity)
    return flux.FluxRenderer(old.pipe, old.processor, old.classifier, old.nsfw_index,
                             old.torch, old.info, identity=identity)


class TokenCount:
    model_max_length = 77

    def __init__(self, counts):
        self.counts = counts

    def __call__(self, text, add_special_tokens=False):
        return types.SimpleNamespace(input_ids=[0] * self.counts(text, add_special_tokens))


def test_flux_accepts_description_that_fits_clip_and_t5_individually(tmp_path, monkeypatch):
    stub_pil(monkeypatch)
    r = renderer()
    r.pipe.tokenizer = TokenCount(lambda text, special: 69)
    r.pipe.tokenizer_2 = TokenCount(lambda text, special: 81 + int(special))
    got = r.render(request(tmp_path))
    assert not got['flagged'] and len(r.pipe.calls) == 1
    assert (tmp_path/'o.jpg').is_file()


@pytest.mark.parametrize('clip,t5', [(76, 100), (60, 513)])
def test_flux_refuses_required_text_exceeding_either_encoder(clip, t5):
    r = renderer()
    r.pipe.tokenizer = TokenCount(lambda text, special: clip)
    r.pipe.tokenizer_2 = TokenCount(lambda text, special: t5)
    with pytest.raises(PromptTooLong):
        r._fit((('adult in a coat', 0),))
    assert not r.pipe.calls


def test_flux_drops_optional_text_at_t5_boundary_including_eos():
    r = renderer()
    r.pipe.tokenizer = TokenCount(lambda text, special: 50)
    r.pipe.tokenizer_2 = TokenCount(
        lambda text, special: (512 if 'background' in text else 511) + int(special))
    kept, dropped = r._fit((('adult in a coat', 0), ('background', 1)))
    assert kept == 'adult in a coat' and dropped == ('background',)


def test_sdxl_still_enforces_both_clip_encoder_windows():
    r = make_renderer(make_torch())
    r.pipe.tokenizer = TokenCount(lambda text, special: 69)
    r.pipe.tokenizer_2 = TokenCount(lambda text, special: 81)
    with pytest.raises(PromptTooLong):
        r._fit((('adult in a coat', 0),))


@pytest.mark.parametrize('score,flagged', [(0.05, False), (0.2, True), (0.9, True)])
def test_flux_keeps_negative_guidance_and_shared_image_gate(tmp_path, monkeypatch, score, flagged):
    stub_pil(monkeypatch)
    r = renderer(score=score)
    result = r.render(request(tmp_path))
    call = r.pipe.calls[0]
    assert call['true_cfg_scale'] == 1.5 and call['guidance_scale'] == 6.0
    assert call['negative_prompt'] == 'blurry' and call['max_sequence_length'] == 512
    assert result['flagged'] is flagged
    assert (tmp_path/'o.jpg').exists() is (not flagged)
    assert (tmp_path/'t.jpg').exists() is (not flagged)


def test_flux_reference_embeddings_stay_separate_and_plain_render_resets_scale(tmp_path, monkeypatch):
    stub_pil(monkeypatch)
    ref = tmp_path/'ref.jpg'
    ref.write_bytes(JPEG)
    r = renderer(identity=True)
    identity = Identity(str(ref), (0, 0, 1, 1), 0.4)
    for seed in (1, 2):
        r.render(request(tmp_path, identity=identity, seed=seed))
    r.render(request(tmp_path))
    assert r.pipe.embed_calls == 2  # reference and cached negative/blank image
    assert all('do_classifier_free_guidance' not in kw for kw in r.pipe.embedded)
    assert all(kw['grad_off'] for kw in r.pipe.embedded)
    first, second, plain = r.pipe.calls
    assert first['ip_adapter_image_embeds'] == second['ip_adapter_image_embeds']
    assert first['ip_adapter_image_embeds'] != first['negative_ip_adapter_image_embeds']
    assert plain['ip_adapter_image_embeds'] == first['negative_ip_adapter_image_embeds']
    assert r.pipe.scales == [0.4, 0.4, 0.0]


def test_detail_uses_flux_img2img_over_the_same_components(tmp_path, monkeypatch):
    r = renderer()
    r.pipe.components = {'transformer': object(), 'vae': object()}
    calls = []
    i2i = FakePipe(r.torch)
    i2i.enable_model_cpu_offload = lambda: calls.append('offload')
    i2i.set_progress_bar_config = lambda **kw: None
    def create(**components):
        assert components == r.pipe.components
        calls.append('create')
        return i2i
    monkeypatch.setitem(sys.modules, 'diffusers', types.SimpleNamespace(FluxImg2ImgPipeline=create))
    r._draw(r._img2img(), guidance_scale=4, negative_prompt='blurry', image=object())
    assert r._img2img() is i2i and calls == ['create', 'offload']
    assert i2i.calls[0]['true_cfg_scale'] == 1.5
    assert i2i.calls[0]['guidance_scale'] == 4


@pytest.mark.parametrize('guidance', [1.0, 3.5, 5.0, 12.0])
def test_native_guidance_never_becomes_large_true_cfg(guidance):
    r = renderer()
    r._draw(r.pipe, guidance_scale=guidance, negative_prompt='blurry')
    call = r.pipe.calls[0]
    assert call['guidance_scale'] == guidance
    assert call['true_cfg_scale'] == (1.0 if guidance == 1 else 1.5)
    assert call['negative_prompt'] == 'blurry'


@pytest.mark.parametrize('backend,expected', [('realvisxl', 5.0), ('flux1-dev', 3.5)])
def test_smoke_and_detail_use_backend_guidance_defaults(tmp_path, monkeypatch, backend, expected):
    from leeroy_selfie import __main__ as cli
    seen = []
    def draw(req):
        seen.append(req.cfg)
        return dict(flagged=False, seconds=1, peak_gib=1, nsfw=0.01, tokens=30)
    monkeypatch.setattr(render, 'load', lambda *args, **kw: types.SimpleNamespace(render=draw))
    assert cli.main(['smoke', '--backend', backend, '--out', str(tmp_path)]) == 0
    assert seen == [expected]
    monkeypatch.setattr(cli, 'cmd_detail', lambda args: seen.append(args.cfg) or 0)
    args = ['detail', '--backend', backend, '--in', str(tmp_path/'in.jpg'),
            '--out', str(tmp_path/'out.jpg')]
    assert cli.main(args) == 0
    assert seen[-1] == expected
    assert cli.main([*args, '--cfg', '4.5']) == 0
    assert seen[-1] == 4.5


def stack(monkeypatch, *, free=20, labels=None):
    torch = make_torch(free_gib=free)
    torch.bfloat16 = 'bf16'
    torch.cuda.is_bf16_supported = lambda: True
    log = []
    pipe = types.SimpleNamespace()
    pipe.enable_model_cpu_offload = lambda: log.append(('offload',))
    pipe.vae = types.SimpleNamespace(enable_slicing=lambda:None, enable_tiling=lambda:None)
    pipe.set_progress_bar_config = lambda **kw: None
    pipe.load_ip_adapter = lambda path, **kw: log.append(('adapter', path, kw))
    def loader(name, obj):
        def load(path, **kw):
            log.append((name, path, kw))
            return obj
        return types.SimpleNamespace(from_pretrained=load)
    classifier = types.SimpleNamespace(config=types.SimpleNamespace(id2label=labels or {0:'safe',1:'nsfw'}))
    classifier.eval = lambda: classifier
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'diffusers', types.SimpleNamespace(
        __version__='test', BitsAndBytesConfig=lambda **kw:kw,
        FluxTransformer2DModel=loader('transformer',object()), FluxPipeline=loader('pipe',pipe)))
    monkeypatch.setitem(sys.modules, 'transformers', types.SimpleNamespace(
        BitsAndBytesConfig=lambda **kw:kw, T5EncoderModel=loader('text',object()),
        CLIPVisionModelWithProjection=loader('encoder',object()),
        CLIPImageProcessor=loader('image_processor',object()),
        AutoImageProcessor=loader('classifier_processor',object()),
        AutoModelForImageClassification=loader('classifier',classifier)))
    checked=[]
    monkeypatch.setattr(models,'check', lambda data,name,quick=False:checked.append(name) or [(True,'ok')])
    monkeypatch.setattr(flux,'_face_ref',lambda data:None)
    return torch,log,checked


@pytest.mark.parametrize('identity', [False, True])
def test_load_checks_backend_pins_and_loads_only_local_nf4_components(tmp_path,monkeypatch,identity):
    torch,log,checked=stack(monkeypatch)
    r=render.load(tmp_path,min_free_gib=13,identity=identity,backend='flux1-dev')
    assert isinstance(r,flux.FluxRenderer) and r.identity is identity
    assert checked == list(models.FLUX_BASE_PINS + (models.FLUX_IDENTITY_PINS if identity else ()))
    for entry in log:
        if len(entry)==3:
            assert entry[2]['local_files_only'] is True
    for name in ('transformer','text'):
        kwargs=next(row[2] for row in log if row[0]==name)
        assert kwargs['quantization_config']['load_in_4bit'] is True
        assert kwargs['quantization_config']['bnb_4bit_quant_type']=='nf4'
    assert any(row[0]=='adapter' for row in log) is identity
    assert r.info['versions']['backend']=='flux1-dev'


def test_low_vram_refuses_before_loading_and_retry_can_then_load(tmp_path,monkeypatch):
    torch,log,_=stack(monkeypatch,free=2)
    with pytest.raises(render.ReadyError) as error:
        render.load(tmp_path,min_free_gib=13,backend='flux1-dev')
    assert error.value.reason=='vram' and not log
    torch.cuda.mem_get_info=lambda:(20*2**30,24*2**30)
    assert isinstance(error.value.retry(),flux.FluxRenderer)


def test_missing_classifier_label_cannot_make_a_ready_flux_worker(tmp_path,monkeypatch):
    stack(monkeypatch,labels={0:'safe',1:'unknown'})
    with pytest.raises(render.ReadyError,match='model'):
        render.load(tmp_path,min_free_gib=13,backend='flux1-dev')


def test_authenticated_fetch_preserves_login_location_and_default_fetch_stays_sdxl(tmp_path,monkeypatch):
    monkeypatch.setenv('HF_HOME',str(tmp_path/'operator-login'))
    seen=[]
    def download(repo,**kw):
        seen.append((repo,kw))
        name=next(name for name,pin in models.MODEL_PINS.items() if pin.repo==repo)
        put(tmp_path,name)
    monkeypatch.setattr(models,'_snapshot_download',download)
    models.fetch_models(tmp_path,only=['flux1-dev'],authenticated=True,say=lambda text:None)
    assert os.environ['HF_HOME']==str(tmp_path/'operator-login')
    assert seen[0][1]['token'] is True
    seen.clear()
    models.fetch_models(tmp_path,say=lambda text:None)
    assert {repo for repo,_ in seen}=={models.MODEL_PINS[n].repo for n in models.DEFAULT_PINS}
    assert all(kw['token'] is False for _,kw in seen)


def test_cli_flux_fetch_and_doctor_select_flux_not_sdxl(monkeypatch,capsys):
    seen=[]
    monkeypatch.setattr(models,'fetch_models',lambda data,only,**kw:seen.append((only,kw)) or [])
    assert main(['fetch-models','--backend','flux1-dev','--auth'])==0
    assert seen==[(set(models.FLUX_BASE_PINS+models.FLUX_IDENTITY_PINS+models.FACE_PINS
                      +models.EMBEDDING_PINS),
                   {'authenticated':True})]
    assert main(['doctor','--backend','flux1-dev','--quick'])==0
    output=capsys.readouterr().out
    assert 'flux1-dev' in output and 'realvisxl' not in output
    assert 'ipadapter: not fetched' in output
