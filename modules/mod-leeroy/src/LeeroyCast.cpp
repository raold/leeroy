// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyCast.h"
#include "LeeroyModule.h"

#include "AiFactory.h"
#include "CharacterCache.h"
#include "Guild.h"
#include "GuildMgr.h"
#include "Log.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "ScriptMgr.h"

#include <mutex>
#include <utility>

bool LeeroyCastRegistry::Refresh(uint32 guildId, uint32 humanGuid)
{
    std::unordered_set<uint32> guids;
    if (Guild const* guild = sGuildMgr->GetGuildById(guildId))
    {
        _guildMissing = false;
        for (auto const& [lowGuid, member] : guild->GetUnorderedMembers())
            if (Leeroy::IsCastEligible(lowGuid, member.GetLevel(), humanGuid))
                guids.insert(lowGuid);
    }
    else if (!_guildMissing)
    {
        _guildMissing = true;
        LOG_WARN("module.leeroy", "mod-leeroy: guild {} (Leeroy.GuildId) not found, the cast is empty", guildId);
    }

    std::unique_lock<std::shared_mutex> lock(_mutex);
    if (guids == _guids)
        return false;
    _guids = std::move(guids);
    return true;
}

bool LeeroyCastRegistry::IsCast(uint32 lowGuid) const
{
    std::shared_lock<std::shared_mutex> lock(_mutex);
    return _guids.find(lowGuid) != _guids.end();
}

std::unordered_set<uint32> LeeroyCastRegistry::Guids() const
{
    std::shared_lock<std::shared_mutex> lock(_mutex);
    return _guids;
}

std::vector<Leeroy::CastMember> LeeroyCastRegistry::BuildMembers(uint32 guildId) const
{
    std::vector<Leeroy::CastMember> members;
    Guild const* guild = sGuildMgr->GetGuildById(guildId);
    if (!guild)
        return members;

    std::unordered_set<uint32> const guids = Guids();
    members.reserve(guids.size());
    for (uint32 lowGuid : guids)
    {
        ObjectGuid const guid = ObjectGuid::Create<HighGuid::Player>(lowGuid);
        Guild::Member const* entry = guild->GetMember(guid);
        if (!entry)
            continue;   // left the guild since the last Refresh

        Leeroy::CastMember member;
        member.guid = lowGuid;
        member.name = entry->GetName();
        member.cls = entry->GetClass();
        member.gender = entry->GetGender();
        member.level = entry->GetLevel();
        member.rank = entry->GetRankId();
        member.zone = entry->GetZoneId();
        if (CharacterCacheEntry const* cache = sCharacterCache->GetCharacterCacheByGuid(guid))
            member.race = cache->Race;

        Player* player = ObjectAccessor::FindConnectedPlayer(guid);
        if (player && player->IsInWorld())
        {
            member.online = true;
            member.map = static_cast<int32>(player->GetMapId());
            member.zone = player->GetZoneId();
            member.level = player->GetLevel();
            member.specTab = AiFactory::GetPlayerSpecTab(player);
        }
        members.push_back(std::move(member));
    }
    return members;
}

void LeeroyCastRegistry::MarkDirty()
{
    _dirty.store(true);
}

bool LeeroyCastRegistry::ConsumeDirty()
{
    return _dirty.exchange(false);
}

void LeeroyCastRegistry::RequestPush()
{
    _push.store(true);
}

bool LeeroyCastRegistry::ConsumePush()
{
    return _push.exchange(false);
}

class LeeroyCastGuildScript : public GuildScript
{
public:
    LeeroyCastGuildScript() : GuildScript("LeeroyCastGuildScript",
        { GUILDHOOK_ON_ADD_MEMBER, GUILDHOOK_ON_REMOVE_MEMBER })
    {
    }

    void OnAddMember(Guild* guild, Player* /*player*/, uint8& /*plRank*/) override
    {
        MarkIfCastGuild(guild);
    }

    void OnRemoveMember(Guild* guild, Player* /*player*/, bool /*isDisbanding*/, bool /*isKicked*/) override
    {
        MarkIfCastGuild(guild);
    }

private:
    static void MarkIfCastGuild(Guild* guild)
    {
        LeeroyModule& module = LeeroyModule::Instance();
        if (guild && guild->GetId() == module.GuildId())
            module.Cast().MarkDirty();
    }
};

class LeeroyCastPlayerScript : public PlayerScript
{
public:
    LeeroyCastPlayerScript() : PlayerScript("LeeroyCastPlayerScript", { PLAYERHOOK_ON_LEVEL_CHANGED })
    {
    }

    // Can run on a map thread (experience from a kill); MarkDirty is atomic.
    void OnPlayerLevelChanged(Player* player, uint8 /*oldLevel*/) override
    {
        LeeroyModule& module = LeeroyModule::Instance();
        if (player->GetGuildId() == module.GuildId() && player->GetLevel() >= Leeroy::kCastLevel)
            module.Cast().MarkDirty();
    }
};

void AddSC_leeroy_cast()
{
    new LeeroyCastGuildScript();
    new LeeroyCastPlayerScript();
}
