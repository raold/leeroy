// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyCommands.h"
#include "LeeroyCastModel.h"
#include "LeeroyEmotes.h"
#include "LeeroyEvents.h"
#include "LeeroyModule.h"
#include "LeeroyText.h"

#include "CharacterCache.h"
#include "EmoteAction.h"
#include "Guild.h"
#include "GuildMgr.h"
#include "Log.h"
#include "ObjectAccessor.h"
#include "Opcodes.h"
#include "Player.h"
#include "Playerbots.h"
#include "SharedDefines.h"
#include "SocialMgr.h"
#include "WorldPacket.h"
#include "WorldSession.h"

#include <cstddef>
#include <cstdint>
#include <exception>
#include <memory>
#include <optional>
#include <string_view>
#include <unordered_map>
#include <utility>

// Every command runs on the world thread (LeeroyWorldScript::OnUpdate), where no map updates run, and returns its
// ack's error: empty for success, else one of Leeroy::Err.

namespace
{
// The decoder limits say and human_say to the seven channels handled below, so this is never sent.
constexpr std::string_view kBadChannel = "bad field: channel";
// Client and DB limits: a guild MOTD is 128 bytes, a friend note 48.
constexpr std::size_t kMaxMotdBytes = 128;
constexpr std::size_t kMaxFriendNoteBytes = 48;

// World thread only: registered from OnStartup, read by LeeroyExecuteCommand.
std::unordered_map<std::string, LeeroyCommandHandler>& Handlers()
{
    static std::unordered_map<std::string, LeeroyCommandHandler> handlers;
    return handlers;
}

std::string ExecuteExtension(Leeroy::Command const& cmd, std::int64_t now)
{
    auto const it = Handlers().find(cmd.extType);
    if (it == Handlers().end())
        return Leeroy::EncodeAck(now, cmd.id, false, std::string(Leeroy::Err::UnknownType) + cmd.extType);
    try
    {
        LeeroyCommandResult const result = it->second(cmd.raw);
        return Leeroy::EncodeAck(now, cmd.id, result.ok, result.error);
    }
    catch (std::exception const& e)
    {
        LOG_ERROR("module.leeroy", "mod-leeroy: {} handler threw on command {}: {}", cmd.extType, cmd.id, e.what());
    }
    catch (...)
    {
        LOG_ERROR("module.leeroy", "mod-leeroy: {} handler threw on command {}", cmd.extType, cmd.id);
    }
    return Leeroy::EncodeAck(now, cmd.id, false, Leeroy::Err::HandlerError);
}

ObjectGuid PlayerGuid(uint32 lowGuid)
{
    return ObjectGuid::Create<HighGuid::Player>(lowGuid);
}

// The human, when he is in the world: a STATUS_LOGGEDIN packet queued for a player out of the world is dropped
// unhandled (WorldSession.cpp:459).
Player* FindHuman()
{
    Player* human = ObjectAccessor::FindConnectedPlayer(PlayerGuid(LeeroyModule::Instance().HumanGuid()));
    return human && human->IsInWorld() ? human : nullptr;
}

// A cast member who can act: in the world and run by playerbots. `error` is empty when found.
struct Speaker
{
    Player* bot = nullptr;
    PlayerbotAI* botAI = nullptr;
    std::string_view error;
};

Speaker FindSpeaker(uint32 lowGuid)
{
    if (!LeeroyModule::Instance().Cast().IsCast(lowGuid))
        return {nullptr, nullptr, Leeroy::Err::NotCast};
    Player* bot = ObjectAccessor::FindConnectedPlayer(PlayerGuid(lowGuid));
    PlayerbotAI* botAI = bot && bot->IsInWorld() ? GET_PLAYERBOT_AI(bot) : nullptr;
    if (!botAI)
        return {nullptr, nullptr, Leeroy::Err::Offline};
    return {bot, botAI, {}};
}

// Runs inside the caller's LeeroyBridgeSpeech, so every chat event it causes carries via_bridge.
std::string_view ExecuteSay(Leeroy::Command const& cmd)
{
    Speaker const speaker = FindSpeaker(cmd.guid);
    if (!speaker.error.empty())
        return speaker.error;
    std::string const text = Leeroy::SanitizeChatText(cmd.text);
    if (text.empty())
        return Leeroy::Err::EmptyText;

    LeeroyModule& module = LeeroyModule::Instance();
    Player* bot = speaker.bot;
    switch (cmd.channel)
    {
        // Guild, officer, party and raid lines fire no chat hook (Guild.cpp:2175, PlayerbotAI.cpp:3012), so their
        // events are published here. Party and raid reach only the real players: bots never parse them.
        case Leeroy::Channel::Guild:
        case Leeroy::Channel::Officer:
        {
            Guild* guild = sGuildMgr->GetGuildById(module.GuildId());
            if (!guild)
                return Leeroy::Err::NoGuild;
            if (!guild->GetMember(bot->GetGUID()))
                return Leeroy::Err::NotCast;   // left the guild since the last Refresh
            guild->BroadcastToGuild(bot->GetSession(), cmd.channel == Leeroy::Channel::Officer, text, LANG_UNIVERSAL);
            LeeroyPublishChat(bot, cmd.channel, text, 0);
            return {};
        }
        case Leeroy::Channel::Party:
            if (!speaker.botAI->SayToParty(text))
                return Leeroy::Err::NotInGroup;
            LeeroyPublishChat(bot, cmd.channel, text, 0);
            return {};
        case Leeroy::Channel::Raid:
            if (!speaker.botAI->SayToRaid(text))
                return Leeroy::Err::NotInRaid;
            LeeroyPublishChat(bot, cmd.channel, text, 0);
            return {};
        // The chat hooks publish say, yell and a whisper to the human.
        case Leeroy::Channel::Say:
            bot->Say(text, LANG_UNIVERSAL);
            return {};
        case Leeroy::Channel::Yell:
            bot->Yell(text, LANG_UNIVERSAL);
            return {};
        case Leeroy::Channel::Whisper:
        {
            Player* target = ObjectAccessor::FindConnectedPlayer(PlayerGuid(cmd.toGuid));
            if (!target)
                return Leeroy::Err::TargetOffline;
            // A whisper to anyone else is only published: delivered, playerbots would run the text as a command
            // (Playerbots.cpp:265).
            if (cmd.toGuid == module.HumanGuid())
                bot->Whisper(text, LANG_UNIVERSAL, target);
            else
                LeeroyPublishChat(bot, Leeroy::Channel::Whisper, text, cmd.toGuid);
            return {};
        }
        case Leeroy::Channel::RaidWarning:
        case Leeroy::Channel::Emote:
            break;
    }
    return kBadChannel;
}

// Runs inside the caller's LeeroyBridgeSpeech; the text emote hook publishes the event with via_bridge.
std::string_view ExecuteEmote(Leeroy::Command const& cmd)
{
    Speaker const speaker = FindSpeaker(cmd.guid);
    if (!speaker.error.empty())
        return speaker.error;
    std::optional<uint32> const emote = LeeroyTextEmoteId(cmd.emote);
    if (!emote)
        return Leeroy::Err::UnknownEmote;
    Player* bot = speaker.bot;
    if (!bot->IsAlive())
        return Leeroy::Err::Dead;

    ObjectGuid target = ObjectGuid::Empty;
    if (cmd.targetGuid)
    {
        Player* player = ObjectAccessor::FindConnectedPlayer(PlayerGuid(cmd.targetGuid));
        if (!player)
            return Leeroy::Err::TargetOffline;
        target = player->GetGUID();
    }

    // As PlayerbotAI::PlayEmote (PlayerbotAI.cpp:1945).
    WorldPacket packet(CMSG_TEXT_EMOTE, 16);
    packet << uint32(*emote)
           << EmoteActionBase::GetNumberOfEmoteVariants(static_cast<TextEmotes>(*emote), bot->getRace(),
                  bot->getGender())
           << target;
    bot->GetSession()->HandleTextEmoteOpcode(packet);
    return {};
}

std::optional<uint32> HumanChatType(Leeroy::Channel channel)
{
    switch (channel)
    {
        case Leeroy::Channel::Guild:
            return CHAT_MSG_GUILD;
        case Leeroy::Channel::Officer:
            return CHAT_MSG_OFFICER;
        case Leeroy::Channel::Party:
            return CHAT_MSG_PARTY;
        case Leeroy::Channel::Raid:
            return CHAT_MSG_RAID;
        case Leeroy::Channel::Say:
            return CHAT_MSG_SAY;
        case Leeroy::Channel::Yell:
            return CHAT_MSG_YELL;
        case Leeroy::Channel::Whisper:
            return CHAT_MSG_WHISPER;
        case Leeroy::Channel::RaidWarning:
        case Leeroy::Channel::Emote:
            break;
    }
    return std::nullopt;
}

// Not bridge speech: the human's session handles the packet on its next update, as a line he typed.
std::string_view ExecuteHumanSay(Leeroy::Command const& cmd)
{
    Player* human = FindHuman();
    if (!human)
        return Leeroy::Err::HumanOffline;
    std::string const text = Leeroy::SanitizeChatText(cmd.text);
    if (text.empty())
        return Leeroy::Err::EmptyText;
    if (Leeroy::StartsWithCommandPrefix(text))
        return Leeroy::Err::CommandPrefix;
    std::optional<uint32> const type = HumanChatType(cmd.channel);
    if (!type)
        return kBadChannel;

    // A client may not speak LANG_UNIVERSAL (ChatHandler.cpp:76); the race's own language always passes the skill
    // check, hence the original team.
    uint32 const language = human->GetTeamId(true) == TEAM_HORDE ? LANG_ORCISH : LANG_COMMON;
    // Sanitized too: a NUL inside to_name would end the name early and make the rest the message, unchecked.
    std::string const toName = Leeroy::SanitizeChatText(cmd.toName);

    auto packet = std::make_unique<WorldPacket>(CMSG_MESSAGECHAT, 8 + toName.size() + text.size() + 2);
    *packet << *type << language;
    if (cmd.channel == Leeroy::Channel::Whisper)
        *packet << toName;
    *packet << text;
    human->GetSession()->QueuePacket(packet.release());   // the session frees it
    return {};
}

std::string_view ExecuteSetRank(Leeroy::Command const& cmd)
{
    LeeroyModule& module = LeeroyModule::Instance();
    if (!module.Cast().IsCast(cmd.guid))
        return Leeroy::Err::NotCast;
    // Rank 0 would make a second guild master without demoting the first (Guild.cpp:2381).
    if (cmd.rank == GR_GUILDMASTER)
        return Leeroy::Err::InvalidRank;
    Guild* guild = sGuildMgr->GetGuildById(module.GuildId());
    if (!guild)
        return Leeroy::Err::NoGuild;
    ObjectGuid const guid = PlayerGuid(cmd.guid);
    Guild::Member const* member = guild->GetMember(guid);
    if (!member)
        return Leeroy::Err::NotCast;   // left the guild since the last Refresh
    // Nor is the guild master given another rank: the guild would be left without one.
    if (member->IsRank(GR_GUILDMASTER))
        return Leeroy::Err::InvalidRank;
    if (!guild->ChangeMemberRank(guid, cmd.rank))
        return Leeroy::Err::InvalidRank;   // beyond the guild's lowest rank

    if (Player* human = FindHuman())
        if (guild->GetMember(human->GetGUID()))
            guild->HandleRoster(human->GetSession());
    module.Cast().RequestPush();
    return {};
}

std::string_view ExecuteSetMotd(Leeroy::Command const& cmd)
{
    Guild* guild = sGuildMgr->GetGuildById(LeeroyModule::Instance().GuildId());
    if (!guild)
        return Leeroy::Err::NoGuild;
    guild->SetMOTD(Leeroy::SanitizeChatText(cmd.text, kMaxMotdBytes));   // empty clears it
    return {};
}

std::string_view ExecuteAddFriend(Leeroy::Command const& cmd)
{
    if (!LeeroyModule::Instance().Cast().IsCast(cmd.guid))
        return Leeroy::Err::NotCast;
    Player* human = FindHuman();
    if (!human)
        return Leeroy::Err::HumanOffline;

    ObjectGuid const guid = PlayerGuid(cmd.guid);
    PlayerSocial* social = human->GetSocial();
    if (social->HasFriend(guid))
        return {};
    if (social->GetNumberOfSocialsWithFlag(SOCIAL_FLAG_FRIEND) >= SOCIALMGR_FRIEND_LIMIT)
        return Leeroy::Err::FriendsFull;
    CharacterCacheEntry const* cache = sCharacterCache->GetCharacterCacheByGuid(guid);
    if (!cache)
        return Leeroy::Err::NotCast;   // the character is gone
    if (Player::TeamIdForRace(cache->Race) != human->GetTeamId())
        return Leeroy::Err::EnemyFaction;
    if (!social->AddToSocialList(guid, SOCIAL_FLAG_FRIEND))
        return Leeroy::Err::FriendsFull;
    social->SetFriendNote(guid, Leeroy::SanitizeChatText(cmd.note, kMaxFriendNoteBytes));

    // Online as HandleAddFriendOpcode judges it (Socialhandler.cpp:74).
    Player* friendPlayer = ObjectAccessor::FindConnectedPlayer(guid);
    bool const online = friendPlayer && friendPlayer->IsVisibleGloballyFor(human)
        && !friendPlayer->GetSession()->IsGMAccount();
    sSocialMgr->SendFriendStatus(human, online ? FRIEND_ADDED_ONLINE : FRIEND_ADDED_OFFLINE, guid, false);
    return {};
}

// Player::ToggleAFK has no hook: LeeroyPollAfk publishes the change within a second.
std::string_view ExecuteAfk(Leeroy::Command const& cmd)
{
    Speaker const speaker = FindSpeaker(cmd.guid);
    if (!speaker.error.empty())
        return speaker.error;
    speaker.bot->autoReplyMsg = Leeroy::SanitizeChatText(cmd.message);
    if (speaker.bot->isAFK() != cmd.on)
        speaker.bot->ToggleAFK();
    return {};
}
}

std::string LeeroyExecuteCommand(Leeroy::Command const& cmd)
{
    LeeroyModule& module = LeeroyModule::Instance();
    std::int64_t const now = Leeroy::NowUnixMs();
    std::string_view error;
    switch (cmd.type)
    {
        case Leeroy::CommandType::Ping:
            return Leeroy::EncodePong(now, cmd.id);
        case Leeroy::CommandType::GetCast:
            return Leeroy::EncodeCast(now, module.Cast().BuildMembers(module.GuildId()));
        case Leeroy::CommandType::Extension:
            return ExecuteExtension(cmd, now);
        case Leeroy::CommandType::Say:
        {
            LeeroyBridgeSpeech const speech;
            error = ExecuteSay(cmd);
            break;
        }
        case Leeroy::CommandType::Emote:
        {
            LeeroyBridgeSpeech const speech;
            error = ExecuteEmote(cmd);
            break;
        }
        case Leeroy::CommandType::HumanSay:
            error = ExecuteHumanSay(cmd);
            break;
        case Leeroy::CommandType::SetRank:
            error = ExecuteSetRank(cmd);
            break;
        case Leeroy::CommandType::SetMotd:
            error = ExecuteSetMotd(cmd);
            break;
        case Leeroy::CommandType::AddFriend:
            error = ExecuteAddFriend(cmd);
            break;
        case Leeroy::CommandType::Afk:
            error = ExecuteAfk(cmd);
            break;
    }
    return Leeroy::EncodeAck(now, cmd.id, error.empty(), error);
}

bool LeeroyRegisterCommand(std::string const& type, LeeroyCommandHandler handler)
{
    if (type.empty() || !handler)
        return false;
    auto& handlers = Handlers();
    auto const [it, inserted] = handlers.emplace(type, std::move(handler));
    if (!inserted)
        return false;
    // Stored first so the type is never accepted without a handler. Refused here: a core type name, or a type
    // registered with the protocol directly.
    if (!Leeroy::RegisterExtensionCommand(type))
    {
        handlers.erase(it);
        return false;
    }
    return true;
}
