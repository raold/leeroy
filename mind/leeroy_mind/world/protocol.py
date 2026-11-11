from dataclasses import dataclass
import math
import re
from leeroy_mind.protocol import register_command, register_event

MAX_MEMBERS = 8192
PAGE_SIZE = 100


def uint(value,lo=1,hi=0xFFFFFFFF):
    return type(value) is int and lo <= value <= hi


def token(value):
    return isinstance(value,str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}",value) is not None


@dataclass(frozen=True,slots=True)
class WorldMember:
    guid: int
    account: int
    guild_id: int
    guild_name: str
    name: str
    race: int
    class_: int
    gender: int
    level: int
    rank: int
    online: bool
    map: int
    zone: int

    def __post_init__(self):
        if (not all(uint(v) for v in (self.guid,self.account,self.guild_id))
            or not uint(self.race,1,11) or not uint(self.class_,1,11)
            or not uint(self.gender,0,1) or not uint(self.level,1,255) or not uint(self.rank,0,9)
            or not uint(self.map,0) or not uint(self.zone,0) or type(self.online) is not bool
            or any(not isinstance(s,str) or not 1 <= len(s) <= cap or not s.isprintable()
                   for s,cap in ((self.name,24),(self.guild_name,48)))):
            raise ValueError("world_member_invalid")


@dataclass(frozen=True,slots=True)
class GetWorldRoster:
    id: str
    request: str

    def __post_init__(self):
        if not token(self.id) or not token(self.request):
            raise ValueError("world_request_invalid")


@dataclass(frozen=True,slots=True)
class WorldRoster:
    ts: int
    request: str
    own_guild: int
    human: int
    page: int
    pages: int
    total: int
    members: tuple[WorldMember,...]

    def __post_init__(self):
        if (not token(self.request) or not uint(self.own_guild) or not uint(self.human)
            or not uint(self.ts,0,253402300799999) or not uint(self.pages,1,82)
            or not uint(self.total,0,MAX_MEMBERS) or self.pages != max(1,math.ceil(self.total/PAGE_SIZE))
            or not uint(self.page,0,self.pages-1) or type(self.members) is not tuple
            or len(self.members) != min(PAGE_SIZE,max(0,self.total-self.page*PAGE_SIZE))
            or any(m.guid == self.human or m.guild_id == self.own_guild for m in self.members)
            or len({m.guid for m in self.members}) != len(self.members)):
            raise ValueError("world_roster_invalid")


@dataclass(frozen=True,slots=True)
class WorldSay:
    id: str
    guid: int
    guild_id: int
    channel: str
    text: str
    to_guid: int = 0

    def __post_init__(self):
        if (not token(self.id) or not uint(self.guid) or not uint(self.guild_id)
            or self.channel not in ("trade","general") or self.to_guid != 0
            or not isinstance(self.text,str) or not 1 <= len(self.text.encode("utf-8")) <= 255):
            raise ValueError("world_speech_scope")


def register():
    register_command("get_world_roster",GetWorldRoster)
    register_command("world_say",WorldSay)
    register_event("world_roster",WorldRoster)
