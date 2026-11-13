"""Startup-only voice-note settings; any malformed table disables the feature."""
from dataclasses import dataclass, fields
import math
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class VoiceNotesConfig:
    enabled: bool = False
    share: float = .15
    per_member_day: int = 3
    guild_day: int = 12
    max_words: int = 40


def load_config(path):
    try:
        table=tomllib.loads(Path(path).read_text(encoding='utf-8-sig')).get('voice_notes',{})
        if type(table) is not dict or set(table)-{f.name for f in fields(VoiceNotesConfig)}:
            return VoiceNotesConfig()
        if type(table.get('enabled',False)) is not bool:
            return VoiceNotesConfig()
        share=table.get('share',.15)
        if type(share) not in (int,float) or not math.isfinite(share) or not 0<=share<=.5:
            return VoiceNotesConfig()
        for key,lo,hi,default in (('per_member_day',0,10,3),('guild_day',0,50,12),('max_words',5,60,40)):
            value=table.get(key,default)
            if type(value) is not int or not lo<=value<=hi:
                return VoiceNotesConfig()
        return VoiceNotesConfig(**table)
    except (OSError,ValueError,TypeError):
        return VoiceNotesConfig()
