"""Manual stopped-mind re-rolls. Old galleries and current picks are read-only."""
from dataclasses import asdict, replace
import random

from leeroy_mind.nightly.enrich import _race_class
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.looks_diversity import LooksRefused, protection, sample
from leeroy_mind.personas.schema import ProfilePhoto
from leeroy_mind.personas.screens import has_word, names_real_name
from leeroy_mind.wow import CAST_MIN_LEVEL
from . import files, kind, similarity
from .config import worker_command, worker_env
from .scene_choices import EXPRESSIONS, OUTFITS, PLAIN_PLACES
from .store import photos, queue_restyled
from .style import LUST, style_of
from .worker import SelfieWorker

PENDING = "selfies:variety-pick:"


def choose_photo(p, *, seed, ident, names):
    rng = random.Random(f"variety-photo:{p.guid}:{seed}")
    for _ in range(128):
        if style_of(p) == LUST:
            data = dict(kind=rng.choice(("selfie","mirror_selfie","snapshot")),
                framing=rng.choice(("upper_body","three_quarter")), setting=rng.choice(kind.LUST_SETTINGS),
                outfit=rng.choice(tuple(kind.LUST_OUTFITS)), colour=rng.choice(kind.LUST_COLOURS),
                expression=rng.choice(kind.LUST_EXPRESSIONS))
            if kind.screen_lust(p,data):
                continue
            photo = kind.compose_lust(data)
        else:
            photo = ProfilePhoto(rng.choice(("selfie","mirror_selfie","snapshot","headshot")),
                rng.choice(("face","head_and_shoulders","upper_body","three_quarter")),
                rng.choice(tuple(PLAIN_PLACES.values())), rng.choice(tuple(OUTFITS.values())),
                rng.choice(tuple(EXPRESSIONS.values())))
            if kind.screen_kind(p,asdict(photo),ident=ident,member_names=names):
                continue
        text = " ".join(asdict(photo).values())
        if not names_real_name(text,ident) and not any(has_word(text,n) for n in names if n):
            return photo
    raise LooksRefused("photo_no_safe_choice")


def cast_personas(store, human):
    return {g:p for g,m in store.cast().items() if g != human and m.level >= CAST_MIN_LEVEL
            and (p := load_persona(store,g)) is not None}


async def collect(root,store,picks,cfg,clock,*,persist,make_worker=None):
    vectors,skipped = {},[]
    worker = None
    try:
        for guid,pick in sorted(picks.items()):
            with store.transaction():
                value = similarity.cached(store.conn,guid,pick.pick)
            if value is None:
                ref = files.history(root,guid,pick.pick)
                if not ref.is_file() or not ref.resolve().is_relative_to(root.resolve()):
                    skipped.append({"guid":guid,"code":"photo_unavailable"})
                    continue
                if worker is None:
                    worker = (make_worker or SelfieWorker)(worker_command(cfg,embedding=True),env=worker_env(cfg))
                    await worker.start()
                value = await worker.embed(ref)
                if persist:
                    similarity.save(store,guid,pick.pick,value,clock.now())
            vectors[guid] = value
    finally:
        if worker is not None:
            await worker.stop()
    return vectors,skipped


async def run(root,store,cfg,ident,clock,*,top=30,go=False,make_worker=None):
    with store.transaction():
        personas = cast_personas(store,ident.guid)
        picks = {g:p for g,p in photos(store).items() if g in personas}
        versions = store.persona_versions()
        protected = {g:why for g in personas if (why := protection(store.conn,g))}
        pending = {g for g,p in picks.items() if store.get_meta(PENDING+str(g)) == str(p.pick)}
    if len(picks) > similarity.MAX_PHOTOS:
        raise LooksRefused("too_many_photos")
    vectors,skipped = await collect(root,store,picks,cfg,clock,persist=go,make_worker=make_worker)
    selected,kept,pairs = similarity.choose(vectors,protected,similarity.MAX_PHOTOS)
    selected = [item for item in selected if item[2] not in pending][:top]
    skipped.extend({"guid":g,"code":"awaiting_new_pick"} for g in sorted(pending))
    report = {"dry_run":not go,"embedded":len(vectors),"selected":[],"kept":kept,
              "protected":[{"guid":g,"code":why} for g,why in sorted(protected.items())],
              "skipped":skipped,"pairs":[{"left":a,"right":b,"distance":round(d,6)} for d,a,b in pairs[:30]]}
    names = tuple(m.name for m in store.cast().values()) + tuple(p.identity.real_name for p in personas.values())
    for size,distance,guid,keeper in selected:
        p = personas[guid]
        try:
            appearance = sample(p,personas.values(),seed=versions[guid],ident=ident,names=names)
            changed = replace(p,appearance=appearance)
            changed = replace(changed,photo=choose_photo(changed,seed=versions[guid],ident=ident,names=names))
            if go:
                with store.transaction():
                    if (protection(store.conn,guid) or store.persona_versions().get(guid) != versions[guid]
                            or photos(store).get(guid) != picks[guid]):
                        raise LooksRefused("persona_or_pick_changed")
                    race,cls = _race_class(store,guid)
                    save_persona(store,changed,source="variety",ts=clock.now(),race=race,class_name=cls)
                    kind._record(store,guid,style_of(changed),changed.photo)
                    queue_restyled(store,guid,now=clock.now())
                    store.set_meta(PENDING+str(guid),str(picks[guid].pick))
            personas[guid] = changed  # subsequent planned looks must also stay distinct
            report["selected"].append({"guid":guid,"keeper":keeper,"cluster_size":size,
                "distance":round(distance,6),"code":"rerolled" if go else "would_reroll"})
        except LooksRefused as exc:
            report["skipped"].append({"guid":guid,"code":str(exc)})
    return report
