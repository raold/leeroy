// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmModule.h"
#include "RealmPorts.h"
#include "Chat.h"
#include "ChatCommand.h"
#include "Log.h"
#include "ObjectMgr.h"
#include "ScriptMgr.h"
#include <algorithm>
#include <atomic>
#include <deque>
#include <mutex>
#include <set>

namespace Leeroy::Realm
{
namespace
{
struct KickFact { std::uint32_t guild, human, target; std::uint64_t at; };
std::mutex factsMutex;
std::deque<KickFact> kicks;
std::set<std::uint32_t> deleted;
std::atomic<bool> dirty{true}, unknown{false};
std::uint32_t remaining = 0;
thread_local bool commandCallThrough = false;

void Changed()
{
    dirty = true;
    LeeroyModule::Instance().Cast().MarkDirty();
}

void Confirmed(KickFact fact)
{
    std::lock_guard lock(factsMutex);
    if (kicks.size() >= 1024)
        unknown = true; // Missing removal authority must never turn into a re-add.
    else
        kicks.push_back(fact);
    Changed();
}

char const* ActionName(GuardAction action)
{
    switch (action)
    {
        case GuardAction::Readd: return "readd";
        case GuardAction::LeaveBotGuildThenReadd: return "readd";
        case GuardAction::HisKick: return "his_kick";
        case GuardAction::Gone: return "gone";
        case GuardAction::Blocked: return "blocked";
        case GuardAction::Failed: return "failed";
    }
    return "failed";
}

class RealmGuardGuildScript : public GuildScript
{
public:
    RealmGuardGuildScript() : GuildScript("LeeroyRealmGuardGuildScript",
        {GUILDHOOK_ON_ADD_MEMBER, GUILDHOOK_ON_REMOVE_MEMBER, GUILDHOOK_ON_EVENT}) { }
    void OnAddMember(::Guild* guild, Player*, uint8&) override
    {
        if (guild && guild->GetId() == GuildId()) Changed();
        // Generic membership is deliberately not AllowRejoin authority.
    }
    void OnRemoveMember(::Guild* guild, Player*, bool, bool) override
    {
        if (guild && guild->GetId() == GuildId()) Changed();
    }
    void OnEvent(::Guild* guild, uint8 event, ObjectGuid::LowType kicker,
                 ObjectGuid::LowType removed, uint8) override
    {
        auto const configuredGuild = GuildId(), human = HumanGuid();
        if (guild && UninviteEventIsHis(event == GUILD_EVENT_LOG_UNINVITE_PLAYER,
                guild->GetId(), kicker, configuredGuild, human))
            Confirmed({configuredGuild, human, removed, NowMs()});
    }
};

class RealmGuardPlayerScript : public PlayerScript
{
public:
    RealmGuardPlayerScript() : PlayerScript("LeeroyRealmGuardPlayerScript", {PLAYERHOOK_ON_DELETE}) { }
    void OnPlayerDelete(ObjectGuid guid, uint32) override
    {
        std::lock_guard lock(factsMutex);
        if (deleted.size() >= 1024 && !deleted.contains(guid.GetCounter())) unknown = true;
        else deleted.insert(guid.GetCounter());
        Changed();
    }
};

class RealmGuardCommandScript : public AllCommandScript
{
public:
    RealmGuardCommandScript() : AllCommandScript("LeeroyRealmGuardCommandScript",
        {ALLCOMMANDHOOK_ON_TRY_EXECUTE_COMMAND}) { }
    bool OnTryExecuteCommand(ChatHandler& handler, std::string_view command) override
    {
        if (commandCallThrough)
            return true;
        auto* invoker = handler.GetPlayer();
        auto const human = HumanGuid(), configuredGuild = GuildId();
        if (!CommandIsHis(handler.IsConsole(), invoker ? invoker->GetGUID().GetCounter() : 0, human))
            return true;
        auto target = ParseGuildUninvite(command);
        if (!target)
            return true;
        ObjectGuid guid;
        if (target->name)
        {
            auto name = *target->name;
            if (!normalizePlayerName(name)) return true;
            guid = sCharacterCache->GetCharacterGuidByName(name);
        }
        else if (auto* selected = handler.getSelectedPlayerOrSelf())
            guid = selected->GetGUID();
        ::Guild const* before = sGuildMgr->GetGuildById(configuredGuild);
        auto* guard = RealmModule::Instance().Guard();
        if (guid.IsEmpty() || !before || !before->GetMember(guid) || !guard)
            return true;
        guard->NoteCommandKick(guid.GetCounter(), NowMs());
        // Installed core has only a pre-command hook. Delegate the unchanged
        // command through its normal parser/permissions with guarded reentry,
        // then inspect this exact target synchronously. No direct DeleteMember,
        // no next-tick inference, and no second invocation after exceptions.
        // Additional third-party prehooks can run twice before this wrapper;
        // compatibility must be reviewed when another such hook is installed.
        struct Reentry
        {
            Reentry() { commandCallThrough = true; }
            ~Reentry() { commandCallThrough = false; }
        } scope;
        try
        {
            Acore::ChatCommands::TryExecuteCommand(handler, command);
            auto* after = sGuildMgr->GetGuildById(configuredGuild);
            if (configuredGuild == GuildId() && human == HumanGuid() && after && !after->GetMember(guid))
            {
                guard->NoteKick(guid.GetCounter(), NowMs());
                Changed();
            }
        }
        catch (...)
        {
            unknown = true;
            LOG_ERROR("module.leeroy", "mod-leeroy: cast guard command result unknown; protection mutations suspended");
        }
        return false;
    }
};
}

bool CastGuardFactsUnknown() { return unknown.load(); }

bool CastGuardKickFactPending(std::uint32_t guid)
{
    std::lock_guard lock(factsMutex);
    return std::any_of(kicks.begin(), kicks.end(), [&](KickFact const& fact) { return fact.target == guid; });
}

bool CastRecruitmentProtected(std::uint32_t guid)
{
    auto* guard = RealmModule::Instance().Guard();
    if (!guard || unknown.load() || guard->RecruitmentProtected(guid)) return true;
    auto const configuredGuild = GuildId(), human = HumanGuid();
    std::lock_guard lock(factsMutex);
    return std::any_of(kicks.begin(), kicks.end(), [&](KickFact const& fact)
    { return fact.guild == configuredGuild && fact.human == human && fact.target == guid; });
}

void TickCastGuard(std::uint32_t diff)
{
    auto& module = RealmModule::Instance();
    auto* guard = module.Guard();
    if (!guard)
        return;
    {
        std::lock_guard lock(factsMutex);
        for (auto it = kicks.begin(); it != kicks.end();)
        {
            if (it->guild == GuildId() && it->human == HumanGuid())
            {
                guard->NoteKick(it->target, it->at);
                it = kicks.erase(it);
            }
            else ++it; // Preserve old configured identity's confirmation until it is selected again.
        }
    }
    if (!Config().enable || !Config().castGuard || unknown.load())
        return;
    bool const changed = dirty.exchange(false);
    if (!changed && diff < remaining)
    {
        remaining -= diff;
        return;
    }
    remaining = 5000;
    ::Guild* guild = sGuildMgr->GetGuildById(GuildId());
    if (!guild)
        return;
    GuardInput input{{}, {}, NowMs()};
    for (auto const& [guid, member] : guild->GetUnorderedMembers())
        if (guid != HumanGuid()) input.members.emplace(guid, member.GetRankId());
    for (auto const& [guid, rank] : guard->Known())
    {
        (void)rank;
        if (input.members.contains(guid)) continue;
        bool explicitlyDeleted;
        {
            std::lock_guard lock(factsMutex);
            explicitlyDeleted = deleted.contains(guid);
        }
        if (explicitlyDeleted) input.chars.emplace(guid, CharState{false, 0, false});
        else if (auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(guid)))
        {
            auto* online = FindOnline(guid);
            auto const currentGuild = online ? online->GetGuildId() : character->GuildId;
            input.chars.emplace(guid, CharState{true, currentGuild, currentGuild && !IsRealGuild(currentGuild)});
        }
    }
    for (auto step : guard->Pass(input))
    {
        std::string reason;
        if (step.action == GuardAction::Readd || step.action == GuardAction::LeaveBotGuildThenReadd)
        {
            reason = module.MutationReason(step.guid);
            auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(step.guid));
            auto* online = FindOnline(step.guid);
            auto const currentGuild = online ? online->GetGuildId() : (character ? character->GuildId : 0);
            auto observed = input.chars.find(step.guid);
            if (!character || observed == input.chars.end() || currentGuild != observed->second.guildId)
                reason = "identity_changed";
            if (reason.empty() && currentGuild)
            {
                auto* other = sGuildMgr->GetGuildById(currentGuild);
                if (step.action != GuardAction::LeaveBotGuildThenReadd || IsRealGuild(currentGuild) || !other
                    || !other->GetMember(PlayerGuid(step.guid))) reason = "guild_changed";
                // Core DeleteMember promotes another member or disbands when
                // removing the leader, even with canDeleteGuild=false. This
                // single-body guard pass owns neither of those shared effects.
                else if (other->GetLeaderGUID() == PlayerGuid(step.guid)) reason = "guild_leader";
                else other->DeleteMember(PlayerGuid(step.guid), false, false);
            }
            if (reason.empty()) reason = module.MutationReason(step.guid);
            if (reason.empty())
            {
                guild = sGuildMgr->GetGuildById(GuildId());
                if (!guild) reason = "guild_unavailable";
                else
                {
                    // Zero rights is ambiguous: a valid restricted rank and an
                    // invalid index both return zero. Never scan for a rank
                    // with rights, which could promote this returning member.
                    // Let the core select its actual lowest rank in that case.
                    auto const requestedRank = guild->GetRankRights(step.rank) ? step.rank : uint8(GUILD_RANK_NONE);
                    if (!guild->AddMember(PlayerGuid(step.guid), requestedRank))
                        reason = "readd_failed";
                    else if (auto const* restored = guild->GetMember(PlayerGuid(step.guid)))
                    {
                        step.rank = restored->GetRankId();
                        Changed();
                    }
                    else reason = "readd_unproved";
                }
            }
            if (!reason.empty())
            {
                guard->NoteFailed(step.guid, input.nowMs);
                step.action = GuardAction::Failed;
            }
        }
        // Exactly Publish's encoding; a his_kick's own ts becomes its receipt.
        auto const ts = NowUnixMs();
        LeeroyModule::Instance().Publish(EncodeEvent(ts, MakeGuard(step.guid, ActionName(step.action), step.rank, reason)),
            false);
        if (step.action == GuardAction::HisKick && ts > 0)
            guard->NoteKickReceipt(step.guid, static_cast<std::uint64_t>(ts), step.rank);
        LOG_INFO("module.leeroy", "mod-leeroy: cast guard {} {} rank {}", ActionName(step.action), step.guid, uint32(step.rank));
    }
}

void AddCastGuardScripts()
{
    new RealmGuardGuildScript();
    new RealmGuardPlayerScript();
    new RealmGuardCommandScript();
}
}
