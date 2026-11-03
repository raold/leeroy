// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmSpeech.h"
#include "RealmPorts.h"
#include "LeeroyEmotes.h"
#include "LeeroyEvents.h"
#include "LeeroyText.h"

#include "AllMapScript.h"
#include "Channel.h"
#include "ChannelMgr.h"
#include "Chat.h"
#include "DBCStores.h"
#include "EmoteAction.h"
#include "GameEventScript.h"
#include "GlobalScript.h"
#include "InstanceScript.h"
#include "Opcodes.h"
#include "PlayerScript.h"
#include "ScriptMgr.h"
#include "ServerScript.h"
#include "World.h"
#include "WorldPacket.h"
#include "WorldSession.h"
#include "WorldSessionMgr.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <limits>
#include <map>
#include <mutex>
#include <string_view>
#include <vector>

namespace Leeroy::Realm
{
namespace
{
std::string_view ChannelName(std::uint32_t id)
{
    switch (id)
    {
        case ChatChannelId::GENERAL: return "general";
        case ChatChannelId::TRADE: return "trade";
        case ChatChannelId::LOOKING_FOR_GROUP: return "lfg";
        case ChatChannelId::GUILD_RECRUITMENT: return "recruitment";
        default: return {};
    }
}

std::uint32_t ChannelId(std::string_view name)
{
    if (name == "general") return ChatChannelId::GENERAL;
    if (name == "trade") return ChatChannelId::TRADE;
    if (name == "lfg") return ChatChannelId::LOOKING_FOR_GROUP;
    if (name == "recruitment") return ChatChannelId::GUILD_RECRUITMENT;
    return 0;
}

// Immediate world-thread actions still require current native ownership. Speech
// has no movement-purpose gameplay waiver, and Watched alone never authorizes it.
Player* Speaker(std::uint32_t guid, std::string& why)
{
    auto& module = RealmModule::Instance();
    if (!module.On()) why = "realm_off";
    else if (!guid || guid == HumanGuid() || (!IsCast(guid) && !module.IsActor(guid)))
        why = "not_speaker:" + std::to_string(guid);
    else if (auto reason = module.MutationReason(guid); !reason.empty()) why = std::move(reason);
    else
    {
        auto* bot = FindOnline(guid);
        if (!bot || !GET_PLAYERBOT_AI(bot) || IsSelfBot(bot)) why = "offline";
        else if (!bot->IsAlive()) why = "dead";
        else return bot;
    }
    return nullptr;
}

bool SameFactionAsHuman(Player const* bot)
{
    auto const* human = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(HumanGuid()));
    return human && bot->GetTeamId() == Player::TeamIdForRace(human->Race);
}

// Native speech methods return void and can refuse before producing chat. Observe
// only the exact expected packet/session during this one synchronous invocation.
// This proves server acceptance; it does not prove delivery to a human client.
struct SpeechReceipt
{
    WorldSession* session;
    WorldPacket expected;
    bool accepted = false;
};
thread_local SpeechReceipt* currentReceipt = nullptr;
thread_local std::uint32_t deferredChannelGuid = 0;

class ReceiptScope
{
public:
    explicit ReceiptScope(SpeechReceipt& receipt) : _previous(currentReceipt) { currentReceipt = &receipt; }
    ~ReceiptScope() { currentReceipt = _previous; }
    ReceiptScope(ReceiptScope const&) = delete;
    ReceiptScope& operator=(ReceiptScope const&) = delete;
private:
    SpeechReceipt* _previous;
};

class DeferChannelEvent
{
public:
    explicit DeferChannelEvent(std::uint32_t guid) : _previous(deferredChannelGuid) { deferredChannelGuid = guid; }
    ~DeferChannelEvent() { deferredChannelGuid = _previous; }
    DeferChannelEvent(DeferChannelEvent const&) = delete;
    DeferChannelEvent& operator=(DeferChannelEvent const&) = delete;
private:
    std::uint32_t _previous;
};

std::string BuiltInChannelName(Player* bot, std::uint32_t channelId, std::string& why)
{
    auto const* zone = sAreaTableStore.LookupEntry(bot->GetZoneId());
    auto const* entry = sChatChannelsStore.LookupEntry(channelId);
    bool const city = channelId == ChatChannelId::TRADE || channelId == ChatChannelId::GUILD_RECRUITMENT;
    if (city && (!zone || !(zone->flags & AREA_FLAG_SLAVE_CAPITAL)))
    {
        why = "not_in_city";
        return {};
    }
    if (!zone || !entry || !bot->CanJoinConstantChannelInZone(entry, zone))
    {
        why = "not_in_channel";
        return {};
    }
    auto const locale = sWorld->GetAvailableDbcLocale(bot->GetSession()->GetSessionDbcLocale());
    auto const* pattern = entry->pattern[locale];
    if (!pattern || !*pattern)
    {
        why = "not_in_channel";
        return {};
    }
    // The same localized DBC pattern/City area used by PlayerbotMgr::OnBotLogin.
    if (channelId == ChatChannelId::LOOKING_FOR_GROUP)
    {
        std::string result(pattern);
        if (result.size() <= 255) return result;
    }
    else
    {
        auto const* area = city ? sAreaTableStore.LookupEntry(3459) : zone;
        if (area && area->area_name[locale] && *area->area_name[locale])
        {
            std::array<char, 256> name{};
            int const length = std::snprintf(name.data(), name.size(), pattern, area->area_name[locale]);
            if (length > 0 && static_cast<std::size_t>(length) < name.size())
                return std::string(name.data(), static_cast<std::size_t>(length));
        }
    }
    why = "not_in_channel";
    return {};
}

bool LocalSpeechAccepted(Player* bot, ChatMsg kind, std::string const& text, Player* human = nullptr)
{
    // Player::Say/Yell echo to the sender; Player::Whisper sends a normal
    // WHISPER to its target, with the speaker in both GUID fields. FILTERED or
    // a system notification cannot match this packet. Do not invoke chat hooks
    // separately: the normal Player API must enforce them exactly once.
    auto* recipient = kind == CHAT_MSG_WHISPER ? human : bot;
    auto const guid = bot->GetGUID().GetCounter();
    auto const recipientGuid = recipient->GetGUID().GetCounter();
    auto const toGuid = human ? recipientGuid : 0;
    auto const channel = kind == CHAT_MSG_SAY ? Leeroy::Channel::Say
        : kind == CHAT_MSG_YELL ? Leeroy::Channel::Yell : Leeroy::Channel::Whisper;
    auto& module = RealmModule::Instance();
    auto const epoch = module.Epoch();
    auto const speakerIdentity = Identity(guid);
    auto const recipientIdentity = Identity(recipientGuid);
    if (!speakerIdentity || !recipientIdentity || speakerIdentity->player != bot
        || recipientIdentity->player != recipient) return false;
    SpeechReceipt receipt{recipient->GetSession(), WorldPacket{}, false};
    ChatHandler::BuildChatPacket(receipt.expected, kind, LANG_UNIVERSAL, bot, bot, text);
    {
        // The ordinary P1a hooks run before the core's final filter. Defer
        // exactly this tuple until its normal packet supplies the receipt.
        LeeroyDeferredChat defer(guid, channel, toGuid);
        ReceiptScope scope(receipt);
        if (kind == CHAT_MSG_SAY) bot->Say(text, LANG_UNIVERSAL);
        else if (kind == CHAT_MSG_YELL) bot->Yell(text, LANG_UNIVERSAL);
        else bot->Whisper(text, LANG_UNIVERSAL, human);
    }
    // A script-rewritten line is not proof that the requested line was sent.
    if (!receipt.accepted || !module.On() || module.Epoch() != epoch
        || Identity(guid) != speakerIdentity || Identity(recipientGuid) != recipientIdentity) return false;
    std::string why;
    if (Speaker(guid, why) != bot || (human && (HumanGuid() != toGuid || FindOnline(toGuid) != human)))
        return false;
    auto& events = LeeroyModule::Instance();
    if ((events.IsTracked(guid) || (human && events.IsTracked(toGuid)))
        && !(human && LeeroyQuietScope::Active()))
        LeeroyPublishChat(bot, channel, text, toGuid); // The caller's bridge provenance scope remains active.
    return true;
}

LeeroyCommandResult Say(nlohmann::json const& message)
{
    if (!RealmModule::Instance().On()) return {false, "realm_off"};
    std::string why;
    auto const cmd = ParseSay(message, why);
    if (!cmd) return {false, why};
    auto* bot = Speaker(cmd->guid, why);
    if (!bot) return {false, why};
    if (!SameFactionAsHuman(bot)) return {false, "enemy faction"};
    if (!bot->CanSpeak()) return {false, "muted"};
    // Raw Player/Channel methods omit these ordinary session chat gates.
    if (cmd->channel != "whisper" && bot->HasAura(1852)) return {false, "muted"};
    if ((cmd->channel == "say" || cmd->channel == "yell") && bot->IsSpectator())
        return {false, "spectator"};
    std::string text = Leeroy::SanitizeChatText(cmd->text);
    if (text.empty()) return {false, "empty text"};
    if (Leeroy::StartsWithCommandPrefix(text)) return {false, "command prefix"};
    LeeroyBridgeSpeech bridge;
    if (cmd->channel == "say" || cmd->channel == "yell")
    {
        auto const kind = cmd->channel == "say" ? CHAT_MSG_SAY : CHAT_MSG_YELL;
        if (!LocalSpeechAccepted(bot, kind, text)) return {false, "chat_output_unproved"};
    }
    else if (cmd->channel == "whisper")
    {
        if (cmd->toGuid != HumanGuid()) return {false, "bad field: to_guid"};
        auto* human = FindOnline(HumanGuid());
        if (!human) return {false, "human offline"};
        if (human->GetTeamId() != bot->GetTeamId()) return {false, "enemy faction"};
        // As HandleMessagechatOpcode: a silenced player can still contact a GM.
        if (bot->HasAura(1852) && !human->IsGameMaster()) return {false, "muted"};
        if (!LocalSpeechAccepted(bot, CHAT_MSG_WHISPER, text, human)) return {false, "chat_output_unproved"};
    }
    else
    {
        auto const id = ChannelId(cmd->channel);
        auto const name = BuiltInChannelName(bot, id, why);
        if (name.empty()) return {false, why};
        auto* manager = ChannelMgr::forTeam(bot->GetTeamId());
        auto* channel = manager ? manager->GetJoinChannel(name, id) : nullptr;
        if (!channel || channel->GetChannelId() != id) return {false, "not_in_channel"};
        DeferChannelEvent defer(cmd->guid);
        if (!sScriptMgr->OnPlayerCanUseChat(bot, CHAT_MSG_CHANNEL, LANG_UNIVERSAL, text, channel))
            return {false, "chat refused"};
        text = Leeroy::SanitizeChatText(text);
        if (text.empty()) return {false, "empty text"};
        if (Leeroy::StartsWithCommandPrefix(text)) return {false, "command prefix"};
        // Reentrant script hooks cannot grant a now-stale actor permission.
        if (Speaker(cmd->guid, why) != bot) return {false, why.empty() ? "offline" : why};
        if (!bot->CanSpeak() || bot->HasAura(1852)) return {false, "muted"};
        if (!SameFactionAsHuman(bot)) return {false, "enemy faction"};
        if (BuiltInChannelName(bot, id, why) != name) return {false, "not_in_channel"};
        // Join is idempotent for built-in channels. Do not infer success from it.
        channel->JoinChannel(bot, "");
        SpeechReceipt receipt{bot->GetSession(), WorldPacket{}, false};
        ChatHandler::BuildChatPacket(receipt.expected, CHAT_MSG_CHANNEL, LANG_UNIVERSAL, bot, bot, text, 0, name);
        {
            ReceiptScope scope(receipt);
            channel->Say(bot->GetGUID(), text, LANG_UNIVERSAL);
        }
        if (!receipt.accepted) return {false, "not_in_channel"};
        if (RealmModule::Instance().On())
            Publish(MakeChannelChat(cmd->channel, cmd->guid, bot->GetName(), bot->GetZoneId(), text, true), true);
    }
    return {true, {}};
}

LeeroyCommandResult Emote(nlohmann::json const& message)
{
    if (!RealmModule::Instance().On()) return {false, "realm_off"};
    std::string why;
    auto const cmd = ParseEmote(message, why);
    if (!cmd) return {false, why};
    auto* bot = Speaker(cmd->guid, why);
    if (!bot) return {false, why};
    auto const emote = LeeroyTextEmoteId(cmd->emote);
    if (!emote || !sEmotesTextStore.LookupEntry(*emote)) return {false, "unknown emote"};
    if (!bot->CanSpeak()) return {false, "muted"};
    if (bot->IsSpectator()) return {false, "spectator"};
    // The core redirects /ready to a transfer operation in this state.
    if (*emote == 126 && bot->GetMap()->IsPlayerRedirectKickTimerActive()) return {false, "busy:redirect"};
    ObjectGuid target = ObjectGuid::Empty;
    if (cmd->targetGuid)
    {
        auto* player = FindOnline(cmd->targetGuid);
        if (!player || player->GetMap() != bot->GetMap()) return {false, "target offline"};
        target = player->GetGUID();
    }
    LeeroyBridgeSpeech bridge;
    WorldPacket packet(CMSG_TEXT_EMOTE, 16);
    packet << uint32(*emote)
           << EmoteActionBase::GetNumberOfEmoteVariants(static_cast<TextEmotes>(*emote), bot->getRace(), bot->getGender())
           << target;
    bot->GetSession()->HandleTextEmoteOpcode(packet);
    // HandleTextEmoteOpcode applies the normal flood throttle before emitting.
    if (!bot->CanSpeak()) return {false, "muted"};
    return {true, {}};
}

LeeroyCommandResult Announce(nlohmann::json const& message)
{
    auto& module = RealmModule::Instance();
    if (!module.On()) return {false, "realm_off"};
    if (!Config().realmAnnounce) return {false, "quiet"};
    std::string why;
    auto const cmd = ParseAnnounce(message, why);
    if (!cmd) return {false, why};
    auto const kill = module.MatchProof(*cmd, NowMs());
    if (!kill) return {false, "no_kill"};
    auto const* guild = sGuildMgr->GetGuildById(GuildId());
    if (!guild) return {false, "no_guild"};
    auto const boss = Leeroy::SanitizeChatText(cmd->bossName, 48);
    auto const guildName = Leeroy::SanitizeChatText(guild->GetName(), 48);
    if (boss.empty() || guildName.empty()) return {false, "empty text"};
    sWorldSessionMgr->SendServerMessage(SERVER_MSG_STRING,
        FormatAnnouncement(guildName, boss, kill->size, kill->heroic, cmd->attempts, cmd->fightS, cmd->first == "ever"));
    return {true, {}};
}

class RealmChannelScript final : public PlayerScript
{
public:
    RealmChannelScript() : PlayerScript("leeroy_realm_channel", {PLAYERHOOK_CAN_PLAYER_USE_CHANNEL_CHAT}) { }
    bool OnPlayerCanUseChat(Player* player, uint32, uint32, std::string& text, ::Channel* channel) override
    {
        auto& module = RealmModule::Instance();
        if (!module.On()) return true;
        if (!player || !channel || !module.Watched(player->GetGUID().GetCounter())) return true;
        auto const name = ChannelName(channel->GetChannelId());
        if (name.empty()) return true;
        if (LeeroyBridgeSpeech::Active() && deferredChannelGuid == player->GetGUID().GetCounter()) return true;
        // Copies only bounded primitives/text; Publish is thread-safe.
        Publish(MakeChannelChat(name, player->GetGUID().GetCounter(), player->GetName(), player->GetZoneId(),
            Leeroy::SanitizeChatText(std::string_view(text).substr(0, 1024)), LeeroyBridgeSpeech::Active()), true);
        return true;
    }
};

class RealmPacketScript final : public ServerScript
{
public:
    RealmPacketScript() : ServerScript("leeroy_realm_speech_receipt", {SERVERHOOK_ON_PACKET_SENT}) { }
    void OnPacketSent(WorldSession* session, WorldPacket const& packet) override
    {
        if (!RealmModule::Instance().On()) return;
        auto* receipt = currentReceipt;
        if (!receipt || session != receipt->session || receipt->accepted || packet.size() > 1024
            || packet.GetOpcode() != receipt->expected.GetOpcode() || packet.size() != receipt->expected.size()) return;
        if (packet.size() && std::memcmp(packet.contents(), receipt->expected.contents(), packet.size()) == 0)
            receipt->accepted = true;
    }
};

class RealmGameEventScript final : public GameEventScript
{
public:
    RealmGameEventScript() : GameEventScript("leeroy_realm_game_event", {GAMEEVENTHOOK_ON_START, GAMEEVENTHOOK_ON_STOP}) { }
    void OnStart(uint16 id) override
    {
        if (!RealmModule::Instance().On()) return;
        Publish(MakeGameEvent(id, "start"));
    }
    void OnStop(uint16 id) override
    {
        if (!RealmModule::Instance().On()) return;
        Publish(MakeGameEvent(id, "stop"));
    }
};

struct PendingKill
{
    Map* identity = nullptr; // Identity only; dereferenced only through the current map-hook argument.
    std::uint64_t lifecycle = 0;
    std::uint8_t spawnMode = 0;
    Kill kill;
};
constexpr std::size_t kMaxPendingKills = 128;
std::mutex pendingMutex;
std::vector<PendingKill> pendingKills;
std::map<Map*, std::uint64_t> mapLifecycles;
std::uint64_t nextMapLifecycle = 0;

void ForgetMap(Map* map, bool created)
{
    std::lock_guard lock(pendingMutex);
    std::erase_if(pendingKills, [map](PendingKill const& pending) { return pending.identity == map; });
    mapLifecycles.erase(map);
    if (created && map && nextMapLifecycle != std::numeric_limits<std::uint64_t>::max())
        mapLifecycles.emplace(map, ++nextMapLifecycle);
}

class RealmBossScript final : public GlobalScript
{
public:
    RealmBossScript() : GlobalScript("leeroy_realm_boss_candidate", {GLOBALHOOK_ON_BEFORE_SET_BOSS_STATE}) { }
    void OnBeforeSetBossState(uint32 id, EncounterState next, EncounterState old, Map* map) override
    {
        auto& module = RealmModule::Instance();
        if (!module.On()) return;
        if (!map || !map->IsRaid()) return;
        // This hook precedes SetBossState's validation; it is not kill proof.
        auto const epoch = module.Epoch();
        auto const now = NowMs();
        auto const unixNow = NowUnixMs();
        std::uint64_t lifecycle = 0;
        {
            std::lock_guard lock(pendingMutex);
            std::erase_if(pendingKills, [=](PendingKill const& pending) {
                return (pending.identity == map && pending.kill.bossId == id) || pending.kill.nativeEpoch != epoch
                    || now < pending.kill.atMs || now - pending.kill.atMs > kKillVerifyWithinMs;
            });
            auto const life = mapLifecycles.find(map);
            if (life == mapLifecycles.end()) return;
            lifecycle = life->second;
        }
        if (old != IN_PROGRESS || next != DONE || unixNow <= 0) return;
        auto* instance = map->ToInstanceMap();
        if (!instance || !map->GetInstanceId()) return;
        auto const guild = GuildId();
        auto const human = HumanGuid();
        auto const capturedCast = CastMembers(); // Thread-safe native snapshot, before the complete present scan.
        auto const spawnMode = map->GetSpawnMode();
        Kill kill;
        kill.nativeEpoch = epoch; kill.guildId = guild; kill.humanGuid = human;
        kill.map = map->GetId(); kill.instanceId = map->GetInstanceId(); kill.bossId = id;
        kill.difficulty = spawnMode <= 3 ? static_cast<std::int32_t>(spawnMode) : -1;
        kill.size = instance->GetMaxPlayers(); kill.heroic = map->IsHeroic();
        kill.atMs = now; kill.killedAtMs = static_cast<std::uint64_t>(unixNow);
        if (!guild || !human || !ValidRaidMode(kill.difficulty, kill.size, kill.heroic)) return;
        std::vector<PresentKillPlayer> present;
        present.reserve(kMaxKillPlayers);
        for (auto const& reference : map->GetPlayers())
        {
            if (present.size() == kMaxKillPlayers) return; // No prefix, even if the human was already seen.
            auto const* player = reference.GetSource();
            if (!player || !player->IsInWorld() || player->IsDuringRemoveFromWorld() || player->GetMap() != map) return;
            present.push_back({player->GetGUID().GetCounter(), player->GetGuildId()});
        }
        auto participants = QualifyKillParticipants(present, capturedCast, guild, human);
        if (!participants || !module.On() || epoch != module.Epoch() || guild != GuildId() || human != HumanGuid()) return;
        kill.humanPresent = participants->humanPresent;
        kill.castMembers = std::move(participants->castMembers);
        std::lock_guard lock(pendingMutex);
        auto const life = mapLifecycles.find(map);
        if (life == mapLifecycles.end() || life->second != lifecycle || pendingKills.size() >= kMaxPendingKills) return;
        pendingKills.push_back({map, lifecycle, spawnMode, std::move(kill)});
    }
};

class RealmKillMapScript final : public AllMapScript
{
public:
    RealmKillMapScript() : AllMapScript("leeroy_realm_kill_verifier",
        {ALLMAPHOOK_ON_CREATE_MAP, ALLMAPHOOK_ON_DESTROY_MAP, ALLMAPHOOK_ON_MAP_UPDATE}) { }
    // Lifecycle cleanup runs even while disabled; it publishes nothing and never
    // retains a dead map pointer across address/instance-ID reuse.
    void OnCreateMap(Map* map) override { ForgetMap(map, true); }
    void OnDestroyMap(Map* map) override { ForgetMap(map, false); }
    void OnMapUpdate(Map* map, uint32) override
    {
        auto& module = RealmModule::Instance();
        if (!module.On()) return;
        if (!map || !map->IsRaid()) return;
        std::vector<PendingKill> candidates;
        std::uint64_t lifecycle = 0;
        {
            std::lock_guard lock(pendingMutex);
            auto const life = mapLifecycles.find(map);
            if (life == mapLifecycles.end()) return;
            lifecycle = life->second;
            for (auto const& pending : pendingKills)
                if (pending.identity == map && pending.lifecycle == lifecycle) candidates.push_back(pending);
            std::erase_if(pendingKills, [map](PendingKill const& pending) { return pending.identity == map; });
        }
        auto* instance = map->ToInstanceMap();
        auto* script = instance ? instance->GetInstanceScript() : nullptr;
        if (!script) return;
        auto const now = NowMs();
        auto const unixNow = NowUnixMs();
        if (unixNow <= 0) return;
        for (auto const& pending : candidates)
        {
            auto kill = pending.kill;
            if (!module.On() || kill.nativeEpoch != module.Epoch() || now < kill.atMs || now - kill.atMs > kKillVerifyWithinMs
                || kill.guildId != GuildId() || kill.humanGuid != HumanGuid()
                || map->GetId() != kill.map || map->GetInstanceId() != kill.instanceId
                || map->GetSpawnMode() != pending.spawnMode || instance->GetMaxPlayers() != kill.size
                || map->IsHeroic() != kill.heroic) continue;
            // All InstanceScript reads stay on this map's update thread. A load,
            // duplicate DONE, refused transition or intervening reset is no kill.
            if (script->GetBossState(kill.bossId) == DONE)
            {
                kill.verifiedMs = now;
                kill.verifiedAtMs = static_cast<std::uint64_t>(unixNow);
                if (!ValidKillObservation(kill)) continue; // Includes Unix reversal and separate steady bound.
                std::lock_guard lock(pendingMutex);
                auto const life = mapLifecycles.find(map);
                if (life != mapLifecycles.end() && life->second == lifecycle)
                    module.RecordKill(std::move(kill));
            }
        }
    }
};
}

void RegisterRealmSpeechHandlers()
{
    RegisterCommand("realm_say", Say);
    RegisterCommand("realm_emote", Emote);
    RegisterCommand("announce", Announce);
}

void AddRealmSpeechScripts()
{
    new RealmChannelScript();
    new RealmPacketScript();
    new RealmGameEventScript();
    new RealmBossScript();
    new RealmKillMapScript();
}
}
