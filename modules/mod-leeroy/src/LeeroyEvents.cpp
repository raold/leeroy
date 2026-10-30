// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyEvents.h"
#include "LeeroyEmotes.h"
#include "LeeroyModule.h"

#include "Battleground.h"
#include "Creature.h"
#include "DBCStores.h"
#include "Group.h"
#include "Guild.h"
#include "InstanceScript.h"
#include "Item.h"
#include "LootMgr.h"
#include "Map.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "ScriptMgr.h"
#include "World.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

// The game events the mind hears. Every hook filters cheaply, encodes and publishes: no DB, no waiting, nothing of
// another map. Most run on map threads; login, logout and chat typed by a player run on the world thread.

namespace
{
constexpr std::size_t kLootSourceMemory = 512;

thread_local uint32 tBridgeSpeechDepth = 0;

uint32 LowGuid(Object const* object)
{
    return object->GetGUID().GetCounter();
}

std::string_view DbcText(char const* text)
{
    return text ? std::string_view(text) : std::string_view();
}

LocaleConstant DbcLocale()
{
    return sWorld->GetDefaultDbcLocale();
}

std::optional<Leeroy::Channel> ChannelFromChatMsg(uint32 type)
{
    switch (type)
    {
        case CHAT_MSG_SAY:
            return Leeroy::Channel::Say;
        case CHAT_MSG_YELL:
            return Leeroy::Channel::Yell;
        case CHAT_MSG_EMOTE:
            return Leeroy::Channel::Emote;
        case CHAT_MSG_WHISPER:
            return Leeroy::Channel::Whisper;
        case CHAT_MSG_PARTY:
        case CHAT_MSG_PARTY_LEADER:
            return Leeroy::Channel::Party;
        case CHAT_MSG_RAID:
        case CHAT_MSG_RAID_LEADER:
            return Leeroy::Channel::Raid;
        case CHAT_MSG_RAID_WARNING:
            return Leeroy::Channel::RaidWarning;
        case CHAT_MSG_GUILD:
            return Leeroy::Channel::Guild;
        case CHAT_MSG_OFFICER:
            return Leeroy::Channel::Officer;
        default:
            return std::nullopt;
    }
}

// Addon traffic (LANG_ADDON: DBM and friends, the Warden Lua check over guild chat, playerbots' addon whispers) rides
// the same hooks but is not a chat line.
std::optional<Leeroy::Channel> ChatChannel(uint32 type, uint32 language)
{
    if (language == LANG_ADDON)
        return std::nullopt;
    return ChannelFromChatMsg(type);
}

// The last zone published for each tracked GUID. Any thread.
class ZoneMemory
{
public:
    bool Changed(uint32 lowGuid, uint32 zoneId)   // true, and remembered, when zoneId is new for that GUID
    {
        std::lock_guard<std::mutex> lock(_mutex);
        auto const [it, inserted] = _zones.try_emplace(lowGuid, zoneId);
        if (inserted)
            return true;
        if (it->second == zoneId)
            return false;
        it->second = zoneId;
        return true;
    }

private:
    std::mutex _mutex;
    std::unordered_map<uint32, uint32> _zones;
};

// The last kLootSourceMemory loot sources published. Any thread. Keyed by instance too: creature GUID counters are
// generated per map, so two instances of a dungeon can reuse one.
class LootSourceMemory
{
public:
    bool Remember(uint32 instanceId, ObjectGuid source)   // false when the source is already remembered
    {
        Key const key(instanceId, source.GetRawValue());
        std::lock_guard<std::mutex> lock(_mutex);
        for (std::size_t i = 0; i < _count; ++i)
            if (_ring[i] == key)
                return false;
        _ring[_next] = key;
        _next = (_next + 1) % _ring.size();
        if (_count < _ring.size())
            ++_count;
        return true;
    }

private:
    using Key = std::pair<uint32, uint64>;

    std::mutex _mutex;
    std::array<Key, kLootSourceMemory> _ring{};
    std::size_t _next = 0;
    std::size_t _count = 0;
};

ZoneMemory& Zones()
{
    static ZoneMemory zones;
    return zones;
}

LootSourceMemory& LootSources()
{
    static LootSourceMemory sources;
    return sources;
}

// World thread only: the last `afk` value published per GUID; absent means false.
std::unordered_map<uint32, bool>& AfkPublished()
{
    static std::unordered_map<uint32, bool> published;
    return published;
}

void PublishLogin(Player* player, bool login)
{
    LeeroyModule& module = LeeroyModule::Instance();
    uint32 const guid = LowGuid(player);
    if (module.IsTracked(guid))
        module.Publish(Leeroy::EncodeLogin(Leeroy::NowUnixMs(), login, guid, guid == module.HumanGuid()), false);
}

// The map's own thread (or the world thread, when no map updates).
bool AnyTrackedOnMap(Map const* map)
{
    LeeroyModule& module = LeeroyModule::Instance();
    for (MapReference const& ref : map->GetPlayers())
        if (Player* player = ref.GetSource())
            if (module.IsTracked(LowGuid(player)))
                return true;
    return false;
}

// Once per source: epic-or-better items in a dungeon, each with its master-loot slot (its position in loot.items).
// Heard when the looter or anyone else on the map is tracked: in the human's raids the kill credit often goes to an
// addclass bot outside the cast.
void PublishLootDrop(Map const* map, ObjectGuid source, uint32 sourceEntry, Loot const& loot, uint32 looterGuid)
{
    LeeroyModule& module = LeeroyModule::Instance();
    if (!map || !map->IsDungeon() || !module.Enabled())
        return;

    std::vector<Leeroy::LootItem> items;
    for (std::size_t i = 0; i < loot.items.size(); ++i)
    {
        ItemTemplate const* proto = sObjectMgr->GetItemTemplate(loot.items[i].itemid);
        if (!proto || proto->Quality < ITEM_QUALITY_EPIC)
            continue;
        Leeroy::LootItem item;
        item.itemId = proto->ItemId;
        item.name = proto->Name1;
        item.quality = static_cast<std::uint8_t>(proto->Quality);
        item.index = static_cast<uint32>(i);
        items.push_back(std::move(item));
    }
    if (items.empty() || (!module.IsTracked(looterGuid) && !AnyTrackedOnMap(map)))
        return;
    if (!LootSources().Remember(map->GetInstanceId(), source))
        return;

    Leeroy::LootDropEvent e;
    e.map = map->GetId();
    e.instanceId = map->GetInstanceId();
    e.sourceEntry = sourceEntry;
    e.items = std::move(items);
    module.Publish(Leeroy::EncodeLootDrop(Leeroy::NowUnixMs(), e), false);
}

// `group` is the human's group, or nullptr when the human is out of it.
void PublishGroup(Group const* group)
{
    uint32 leader = 0;
    bool raid = false;
    std::vector<uint32> members;
    if (group)
    {
        leader = group->GetLeaderGUID().GetCounter();
        raid = group->isRaidGroup();
        for (Group::MemberSlot const& slot : group->GetMemberSlots())
            members.push_back(slot.guid.GetCounter());
    }
    LeeroyModule::Instance().Publish(Leeroy::EncodeGroup(Leeroy::NowUnixMs(), leader, raid, members), false);
}

ObjectGuid HumanObjectGuid()
{
    return ObjectGuid::Create<HighGuid::Player>(LeeroyModule::Instance().HumanGuid());
}

// Battleground and battlefield raids are temporary: the human's own group is restored afterwards without a hook, so
// following them would leave the mind believing the human has no group.
bool IsBattleGroup(Group const* group)
{
    return group->isBGGroup() || group->isBFGroup();
}

Leeroy::BgWinner Winner(TeamId team)
{
    switch (team)
    {
        case TEAM_HORDE:
            return Leeroy::BgWinner::Horde;
        case TEAM_ALLIANCE:
            return Leeroy::BgWinner::Alliance;
        default:
            return Leeroy::BgWinner::None;
    }
}
}

void LeeroyPublishChat(Player* speaker, Leeroy::Channel channel, std::string const& text, uint32 toGuid)
{
    Leeroy::ChatEvent e;
    e.channel = channel;
    e.fromGuid = LowGuid(speaker);
    e.fromName = speaker->GetName();
    e.toGuid = toGuid;
    e.text = text;
    e.map = speaker->GetMapId();
    e.zone = speaker->GetZoneId();
    e.viaBridge = LeeroyBridgeSpeech::Active();
    LeeroyModule::Instance().Publish(Leeroy::EncodeChat(Leeroy::NowUnixMs(), e), true);
}

LeeroyBridgeSpeech::LeeroyBridgeSpeech()
{
    ++tBridgeSpeechDepth;
}

LeeroyBridgeSpeech::~LeeroyBridgeSpeech()
{
    --tBridgeSpeechDepth;
}

bool LeeroyBridgeSpeech::Active()
{
    return tBridgeSpeechDepth > 0;
}

void LeeroyPollAfk()
{
    LeeroyModule& module = LeeroyModule::Instance();
    std::unordered_map<uint32, bool>& published = AfkPublished();
    std::unordered_set<uint32> guids = module.Cast().Guids();
    guids.insert(module.HumanGuid());
    for (uint32 lowGuid : guids)
    {
        Player* player = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(lowGuid));
        if (!player || !player->IsInWorld())
            continue;
        bool const afk = player->isAFK();
        auto const it = published.find(lowGuid);
        if (afk == (it != published.end() && it->second))
            continue;
        published[lowGuid] = afk;
        module.Publish(Leeroy::EncodeAfk(Leeroy::NowUnixMs(), lowGuid, afk), false);
    }
}

void LeeroyResetAfk()
{
    AfkPublished().clear();
}

class LeeroyEventsPlayerScript : public PlayerScript
{
public:
    LeeroyEventsPlayerScript() : PlayerScript("LeeroyEventsPlayerScript",
        { PLAYERHOOK_ON_LOGIN, PLAYERHOOK_ON_LOGOUT, PLAYERHOOK_CAN_PLAYER_USE_CHAT,
          PLAYERHOOK_CAN_PLAYER_USE_PRIVATE_CHAT, PLAYERHOOK_CAN_PLAYER_USE_GROUP_CHAT,
          PLAYERHOOK_CAN_PLAYER_USE_GUILD_CHAT, PLAYERHOOK_ON_TEXT_EMOTE, PLAYERHOOK_ON_PLAYER_RESURRECT,
          PLAYERHOOK_ON_UPDATE_ZONE, PLAYERHOOK_ON_ACHI_COMPLETE, PLAYERHOOK_ON_GROUP_ROLL_REWARD_ITEM,
          PLAYERHOOK_ON_CREATURE_KILL_CREDIT, PLAYERHOOK_ON_BEFORE_SEND_LOOT })
    {
    }

    void OnPlayerLogin(Player* player) override
    {
        PublishLogin(player, true);
    }

    void OnPlayerLogout(Player* player) override
    {
        PublishLogin(player, false);
    }

    using PlayerScript::OnPlayerCanUseChat;

    // Say, yell and /e (Player::Say/Yell/TextEmote); typed AFK and DND come here too and have no channel.
    bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 language, std::string& msg) override
    {
        std::optional<Leeroy::Channel> const channel = ChatChannel(type, language);
        if (channel && LeeroyModule::Instance().IsTracked(LowGuid(player)))
            LeeroyPublishChat(player, *channel, msg, 0);
        return true;
    }

    // Whispers (Player::Whisper), heard when either side is tracked.
    bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 language, std::string& msg, Player* receiver) override
    {
        std::optional<Leeroy::Channel> const channel = ChatChannel(type, language);
        if (!channel)
            return true;
        LeeroyModule& module = LeeroyModule::Instance();
        uint32 const to = receiver ? LowGuid(receiver) : 0;
        if (module.IsTracked(LowGuid(player)) || (receiver && module.IsTracked(to)))
            LeeroyPublishChat(player, *channel, msg, *channel == Leeroy::Channel::Whisper ? to : 0);
        return true;
    }

    // Party, raid and raid warning typed by a player. Battleground chat has no channel.
    bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 language, std::string& msg, Group* /*group*/) override
    {
        std::optional<Leeroy::Channel> const channel = ChatChannel(type, language);
        if (channel && LeeroyModule::Instance().IsTracked(LowGuid(player)))
            LeeroyPublishChat(player, *channel, msg, 0);
        return true;
    }

    // Guild and officer chat of the cast's guild, from any member.
    bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 language, std::string& msg, Guild* guild) override
    {
        std::optional<Leeroy::Channel> const channel = ChatChannel(type, language);
        if (channel && guild && guild->GetId() == LeeroyModule::Instance().GuildId())
            LeeroyPublishChat(player, *channel, msg, 0);
        return true;
    }

    // HandleTextEmoteOpcode (ChatHandler.cpp:749), before its own DBC check; guid is the target, empty for none.
    void OnPlayerTextEmote(Player* player, uint32 textEmote, uint32 /*emoteNum*/, ObjectGuid guid) override
    {
        LeeroyModule& module = LeeroyModule::Instance();
        uint32 const emoter = LowGuid(player);
        uint32 const target = guid.IsPlayer() ? guid.GetCounter() : 0;
        if (!module.IsTracked(emoter) && !(target && module.IsTracked(target)))
            return;
        std::string_view const name = LeeroyTextEmoteName(textEmote);
        if (name.empty())
            return;
        module.Publish(Leeroy::EncodeTextEmote(Leeroy::NowUnixMs(), emoter, name, target,
            LeeroyBridgeSpeech::Active()), true);
    }

    void OnPlayerResurrect(Player* player, float /*restorePercent*/, bool& /*applySickness*/) override
    {
        LeeroyModule& module = LeeroyModule::Instance();
        uint32 const guid = LowGuid(player);
        if (module.IsTracked(guid))
            module.Publish(Leeroy::EncodeResurrect(Leeroy::NowUnixMs(), guid), false);
    }

    void OnPlayerUpdateZone(Player* player, uint32 newZone, uint32 /*newArea*/) override
    {
        LeeroyModule& module = LeeroyModule::Instance();
        uint32 const guid = LowGuid(player);
        if (!module.Enabled() || !module.IsTracked(guid) || !Zones().Changed(guid, newZone))
            return;
        AreaTableEntry const* area = sAreaTableStore.LookupEntry(newZone);
        std::string_view const name = area ? DbcText(area->area_name[DbcLocale()]) : std::string_view();
        module.Publish(Leeroy::EncodeZone(Leeroy::NowUnixMs(), guid, newZone, name), true);
    }

    void OnPlayerAchievementComplete(Player* player, AchievementEntry const* achievement) override
    {
        LeeroyModule& module = LeeroyModule::Instance();
        uint32 const guid = LowGuid(player);
        if (achievement && module.IsTracked(guid))
            module.Publish(Leeroy::EncodeAchievement(Leeroy::NowUnixMs(), guid, achievement->ID,
                DbcText(achievement->name[DbcLocale()])), false);
    }

    // Need and greed winners (Group.cpp:1664, 1748); disenchant and master loot have no hook.
    void OnPlayerGroupRollRewardItem(Player* player, Item* item, uint32 /*count*/, RollVote voteType,
        Roll* /*roll*/) override
    {
        if (!item || (voteType != NEED && voteType != GREED))
            return;
        ItemTemplate const* proto = item->GetTemplate();
        LeeroyModule& module = LeeroyModule::Instance();
        uint32 const guid = LowGuid(player);
        if (!proto || proto->Quality < ITEM_QUALITY_EPIC || !module.IsTracked(guid))
            return;
        Leeroy::LootMethod const method = voteType == NEED ? Leeroy::LootMethod::Need : Leeroy::LootMethod::Greed;
        module.Publish(Leeroy::EncodeLootWon(Leeroy::NowUnixMs(), guid, proto->ItemId, proto->Name1, method), false);
    }

    // Unit::Kill (Unit.cpp:13858): after the loot fill (:13687), before anyone can open the corpse.
    void OnPlayerCreatureKillCredit(Player* player, Creature* killed) override
    {
        if (killed)
            PublishLootDrop(killed->GetMap(), killed->GetGUID(), killed->GetEntry(), killed->loot, LowGuid(player));
    }

    // Player::SendLoot (Player.cpp:8383), after the loot is filled. Creatures are published at kill time, so only
    // game objects (chests) count here; the source memory keeps it to the first open.
    void OnPlayerBeforeSendLoot(Player* player, ObjectGuid lootGuid, Loot* loot) override
    {
        if (loot && lootGuid.IsGameObject())
            PublishLootDrop(player->GetMap(), lootGuid, lootGuid.GetEntry(), *loot, LowGuid(player));
    }
};

class LeeroyEventsUnitScript : public UnitScript
{
public:
    LeeroyEventsUnitScript() : UnitScript("LeeroyEventsUnitScript", true, { UNITHOOK_ON_UNIT_DEATH })
    {
    }

    // Unit::Kill (Unit.cpp:13925), after the loot fill and kill credit.
    void OnUnitDeath(Unit* unit, Unit* killer) override
    {
        if (!unit || !unit->IsPlayer())
            return;
        LeeroyModule& module = LeeroyModule::Instance();
        uint32 const guid = LowGuid(unit);
        if (!module.IsTracked(guid))
            return;
        uint32 const killerEntry = killer && killer->IsCreature() ? killer->GetEntry() : 0;
        module.Publish(Leeroy::EncodeDeath(Leeroy::NowUnixMs(), guid, unit->GetMapId(), killerEntry), false);
    }
};

class LeeroyEventsGlobalScript : public GlobalScript
{
public:
    LeeroyEventsGlobalScript() : GlobalScript("LeeroyEventsGlobalScript", { GLOBALHOOK_ON_BEFORE_SET_BOSS_STATE })
    {
    }

    // InstanceScript::SetBossState (InstanceScript.cpp:413), on the instance's map thread, before the same-state
    // check; ClassifyBossTransition drops the loading and same-state calls.
    void OnBeforeSetBossState(uint32 id, EncounterState newState, EncounterState oldState, Map* instance) override
    {
        std::optional<Leeroy::BossState> const state = Leeroy::ClassifyBossTransition(newState, oldState);
        if (!state || !instance)
            return;

        LeeroyModule& module = LeeroyModule::Instance();
        Leeroy::BossEvent e;
        bool tracked = false;
        for (MapReference const& ref : instance->GetPlayers())
        {
            Player* player = ref.GetSource();
            if (!player)
                continue;
            uint32 const guid = LowGuid(player);
            tracked = tracked || module.IsTracked(guid);
            e.members.push_back(guid);
        }
        if (!tracked)
            return;

        e.map = instance->GetId();
        e.instanceId = instance->GetInstanceId();
        e.bossId = id;
        // boss_name stays "": script boss ids do not follow DungeonEncounter.dbc order, so no core lookup is sound.
        e.state = *state;
        module.Publish(Leeroy::EncodeBoss(Leeroy::NowUnixMs(), e), false);
    }
};

class LeeroyEventsBattlegroundScript : public AllBattlegroundScript
{
public:
    LeeroyEventsBattlegroundScript() : AllBattlegroundScript("LeeroyEventsBattlegroundScript",
        { ALLBATTLEGROUNDHOOK_ON_BATTLEGROUND_END })
    {
    }

    void OnBattlegroundEnd(Battleground* bg, TeamId winnerTeamId) override
    {
        LeeroyModule& module = LeeroyModule::Instance();
        std::vector<uint32> members;
        for (auto const& entry : bg->GetPlayers())
            if (module.IsTracked(entry.first.GetCounter()))
                members.push_back(entry.first.GetCounter());
        if (!members.empty())
            module.Publish(Leeroy::EncodeBgEnd(Leeroy::NowUnixMs(), static_cast<uint32>(bg->GetBgTypeID()),
                Winner(winnerTeamId), members), false);
    }
};

// Every change involving the human. OnRemoveMember runs after the slot is gone, except when a two-member group is
// about to disband; OnDisband runs before the members leave (Group.cpp:822), so the human is still listed.
class LeeroyEventsGroupScript : public GroupScript
{
public:
    LeeroyEventsGroupScript() : GroupScript("LeeroyEventsGroupScript",
        { GROUPHOOK_ON_ADD_MEMBER, GROUPHOOK_ON_REMOVE_MEMBER, GROUPHOOK_ON_CHANGE_LEADER, GROUPHOOK_ON_DISBAND })
    {
    }

    void OnAddMember(Group* group, ObjectGuid /*guid*/) override
    {
        PublishIfHumanIn(group);
    }

    void OnRemoveMember(Group* group, ObjectGuid guid, RemoveMethod /*method*/, ObjectGuid /*kicker*/,
        char const* /*reason*/) override
    {
        if (IsBattleGroup(group))
            return;
        if (guid == HumanObjectGuid())
            PublishGroup(nullptr);
        else
            PublishIfHumanIn(group);
    }

    void OnChangeLeader(Group* group, ObjectGuid /*newLeaderGuid*/, ObjectGuid /*oldLeaderGuid*/) override
    {
        PublishIfHumanIn(group);
    }

    void OnDisband(Group* group) override
    {
        if (!IsBattleGroup(group) && group->IsMember(HumanObjectGuid()))
            PublishGroup(nullptr);
    }

private:
    static void PublishIfHumanIn(Group const* group)
    {
        if (!IsBattleGroup(group) && group->IsMember(HumanObjectGuid()))
            PublishGroup(group);
    }
};

void AddSC_leeroy_events()
{
    new LeeroyEventsPlayerScript();
    new LeeroyEventsUnitScript();
    new LeeroyEventsGlobalScript();
    new LeeroyEventsBattlegroundScript();
    new LeeroyEventsGroupScript();
}
