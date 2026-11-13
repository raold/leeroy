"""Claim disclosure across media, always bounded by its original audience."""
from leeroy_mind.discord.channels import channel_id, is_discord, is_private, same_medium
from leeroy_mind.discord.layout import channel as layout_channel


def audience_class(channel: str, *, readers: frozenset[int] | None) -> str:
    if is_private(channel):
        return 'pair'
    if channel == 'guild':
        return 'public'
    if channel == 'officer':
        return 'officer'
    if is_discord(channel):
        definition = layout_channel(channel_id(channel))
        if definition is not None:
            return 'officer' if definition.officers_only else 'public'
    return 'other'


def _legacy(source, frm, to, readers, target, speaker, partner, officer):
    if is_private(source) and is_private(target):
        # One mind per member: a whisper and a DM between the same two are one conversation
        if readers is not None and frm != speaker and speaker not in readers:
            return False
        return ({frm, to} == {speaker, partner}
                and (not is_discord(source) or readers is not None and speaker in readers))
    if not same_medium(source, target):
        return False
    if readers is not None and frm != speaker and speaker not in readers:
        return False
    if is_private(source):
        return (source == target and {frm,to} == {speaker,partner}
                and (not is_discord(source) or readers is not None and speaker in readers))
    if source == 'officer':
        return officer and target == 'officer'
    if source == 'guild':
        return True
    if is_discord(source) or source.startswith('voice:'):
        return (source == target and readers is not None and speaker in readers
                and (source != 'voice:officers' or officer))
    return source in {'party','raid','raid_warning','say','yell','emote'} and (
        frm == speaker or readers is not None and speaker in readers)


def claim_visible(*, source_channel: str, source_from: int, source_to: int, source_readers,
                  draft_channel: str, speaker: int, to_guid: int, officer: bool,
                  cross_medium: bool) -> bool:
    legacy = lambda: _legacy(source_channel,source_from,source_to,source_readers,
                             draft_channel,speaker,to_guid,officer)
    if not cross_medium or not (is_discord(source_channel) or is_discord(draft_channel)):
        return legacy()
    src = audience_class(source_channel, readers=source_readers)
    dst = audience_class(draft_channel, readers=None)
    if src == 'other' or dst == 'other':
        return legacy()
    if source_readers is not None and speaker not in source_readers:
        return False
    if is_discord(source_channel) and source_readers is None:
        return False
    if dst == 'officer' and not officer:
        return False
    if src == 'public':
        # Public knowledge may also be mentioned in a narrower conversation.
        return True
    if src == 'officer':
        return dst == 'officer' and officer
    return dst == 'pair' and {source_from,source_to} == {speaker,to_guid}
