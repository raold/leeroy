"""One bounded vision look; answers receive only its screened text."""
import asyncio
import base64
import logging
import re
import time

from leeroy_mind.llm.presets import LOOK
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.logs import log
from leeroy_mind.personas.screens import has_word, name_keys, names_real_name
from leeroy_mind.text import truncate_words

LOOK_PROMPT = ('Describe this photo in two or three plain sentences: what and who is in it '
               '(without guessing who anyone is), where it seems to be, and anything notable '
               'or funny. Do not say who took it or who posted it.')
LOOK_TIMEOUT_S = 30.0


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+|[\r\n]+', text) if s.strip()]


def photo_note(look: str | None) -> str:
    return '[photo: ' + ' '.join(sentences(look)[:2]) + ']' if look else '[photo]'


async def look_at(app, data: bytes) -> str | None:
    started = time.monotonic()
    try:
        if (admission := getattr(app.engine, 'live_admission', None)) is not None:
            await admission()
        async def job(llm):
            return await llm.chat(app.cfg.models.live, [{'role':'user', 'content':LOOK_PROMPT,
                'images':[base64.b64encode(data).decode('ascii')]}], options=dict(LOOK.options),
                think=False, timeout=LOOK_TIMEOUT_S)
        async with asyncio.timeout(LOOK_TIMEOUT_S):
            result = await app.scheduler.submit(Priority.REPLY_HUMAN, job,
                                                deadline=app.clock.now() + LOOK_TIMEOUT_S)
        human = app.engine.human
        names = {human.character_name, human.nickname, human.real_name}
        for member in app.store.cast().values():
            names.add(member.name)
            if (p := app.personas.get(member.guid)) is not None:
                names.update((p.character_name, p.identity.real_name))
        keys = {key for name in names for key in name_keys(name)}
        kept = [s for s in sentences(result.text[:4096]) if not names_real_name(s, human)
                and not any(has_word(s, key) for key in keys)]
        look = truncate_words(' '.join(kept), 600) or None
        log('discord_look', ms=round((time.monotonic()-started)*1000, 1), ok=bool(look), chars=len(look or ''))
        return look
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log('discord_look_failed', level=logging.WARNING, code=type(error).__name__)
        return None
