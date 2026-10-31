"""Who the owner is to the guild. The nickname, real name and pronouns come only from the
local mind.toml; nothing here knows any of them."""
from dataclasses import dataclass

# A bare subject pronoun in the config ("he") reads as its usual pair; anything else is shown
# as written.
_PRONOUN_SETS = {"he": "he/him", "she": "she/her", "they": "they/them"}
THEY_FORMS = ("they", "them", "their")  # unset or unlisted pronouns: nothing is assumed
_FORMS = {"he": ("he", "him", "his"), "she": ("she", "her", "her"), "they": THEY_FORMS}


@dataclass(frozen=True)
class HumanIdentity:
    guid: int
    character_name: str
    nickname: str
    real_name: str
    is_guild_master: bool
    pronouns: str = ""
    race: int = 0  # his character's, from the roster (0: unknown); the guild's faction

    @property
    def called(self) -> str:
        return self.nickname or self.character_name or "the guild master"

    @property
    def _subject(self) -> str:
        return self.pronouns.strip().split("/")[0].casefold()

    @property
    def forms(self) -> tuple[str, str, str]:
        """(subject, object, possessive) for prompt text, from the configured pronouns. An
        unset or unlisted set reads as they/them/their, so nothing is assumed."""
        return _FORMS.get(self._subject, THEY_FORMS)

    def describe(self, include_real_name: bool = False) -> str:
        """One line for prompts. The real name only when asked for: no prompt that a member who
        is not close to him sees may carry it (the live path offers it through
        talk.prompt.real_name_line)."""
        if self.nickname and self.character_name and self.nickname != self.character_name:
            out = f"{self.nickname} (character {self.character_name})"
        else:
            out = self.called
        if self.is_guild_master:
            out += ", the guild master"
        p = self.pronouns.strip()
        if p:
            out += f", {_PRONOUN_SETS.get(p.casefold(), p)}"
        if self.real_name and include_real_name:
            out += f"; close friends know {self.forms[2]} real name, {self.real_name}"
        return out


def human_identity(cfg, store, human_guid: int) -> HumanIdentity:
    """The character name is the one he chats under (meta human_name, kept by the app for the
    guid in meta human_guid), else the stored roster's (pre-flight ruling 6). The rank and
    the race come from the roster; rank 0 is the guild master."""
    m = store.cast().get(human_guid)
    chats_as = (store.get_meta("human_name")
                if human_guid and store.get_meta("human_guid") == str(human_guid) else None)
    return HumanIdentity(
        guid=human_guid, character_name=chats_as or (m.name if m else ""),
        nickname=cfg.human.nickname,
        real_name=cfg.human.real_name, is_guild_master=m is not None and m.rank == 0,
        pronouns=getattr(cfg.human, "pronouns", ""), race=m.race if m else 0)
